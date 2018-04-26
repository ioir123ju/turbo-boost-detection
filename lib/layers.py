import tools.utils as utils
from lib.roialign.roi_align.crop_and_resize import CropAndResizeFunction
from lib.nms.nms_wrapper import nms
import torch.nn.functional as F
from tools.box_utils import *
from tools.image_utils import *


def generate_priors(scales, ratios, shape, feature_stride, anchor_stride):
    """
    EXECUTE ONLY ONCE.
    scales: 1D array of anchor sizes in pixels. Example: [32, 64, 128]
    ratios: 1D array of anchor ratios of width/height. Example: [0.5, 1, 2]
    shape: [height, width] spatial shape of the feature map over which
            to generate anchors.
    feature_stride: Stride of the feature map relative to the image in pixels.
    anchor_stride: Stride of anchors on the feature map. For example, if the
        value is 2 then generate anchors for every other feature map pixel.
    """
    # Get all combinations of scales and ratios
    scales, ratios = np.meshgrid(np.array(scales), np.array(ratios))
    scales = scales.flatten()
    ratios = ratios.flatten()

    # Enumerate heights and widths from scales and ratios
    heights = scales / np.sqrt(ratios)
    widths = scales * np.sqrt(ratios)

    # Enumerate shifts in feature space
    shifts_y = np.arange(0, shape[0], anchor_stride) * feature_stride
    shifts_x = np.arange(0, shape[1], anchor_stride) * feature_stride
    shifts_x, shifts_y = np.meshgrid(shifts_x, shifts_y)

    # Enumerate combinations of shifts, widths, and heights
    box_widths, box_centers_x = np.meshgrid(widths, shifts_x)
    box_heights, box_centers_y = np.meshgrid(heights, shifts_y)

    # Reshape to get a list of (y, x) and a list of (h, w)
    box_centers = np.stack(
        [box_centers_y, box_centers_x], axis=2).reshape([-1, 2])
    box_sizes = np.stack([box_heights, box_widths], axis=2).reshape([-1, 2])

    # Convert to corner coordinates (y1, x1, y2, x2)
    boxes = np.concatenate([box_centers - 0.5 * box_sizes,
                            box_centers + 0.5 * box_sizes], axis=1)
    return boxes


def generate_pyramid_priors(scales, ratios, feature_shapes, feature_strides, anchor_stride):
    """
    EXECUTE ONLY ONCE.
    Generate anchors at different levels of a feature pyramid. Each scale
    is associated with a level of the pyramid, but each ratio is used in all levels of the pyramid.

    Returns:
    anchors: [N, (y1, x1, y2, x2)]. All generated anchors in one array. Sorted
        with the same order of the given scales. So, anchors of scale[0] come
        first, then anchors of scale[1], and so on.
    """
    # Anchors
    # [anchor_count, (y1, x1, y2, x2)]
    anchors = []
    for i in range(len(scales)):
        anchors.append(generate_priors(scales[i], ratios, feature_shapes[i], feature_strides[i], anchor_stride))
    return np.concatenate(anchors, axis=0)


############################################################
#  RPN target layer (previously in __get_item__)
############################################################
def build_rpn_targets(anchors, gt_class_ids, gt_boxes, config):
    """Given the anchors and GT boxes, compute overlaps and identify positive
    anchors and deltas to refine them to match their corresponding GT boxes.

    Args:
        anchors:        [num_anchors, (y1, x1, y2, x2)]
        gt_class_ids:   [num_gt_boxes] Integer class IDs.
        gt_boxes:       [num_gt_boxes, (y1, x1, y2, x2)]
        config:

    Returns:
        rpn_match:      [N] (int32) matches between anchors and GT boxes.
                            1 = positive anchor, -1 = negative anchor, 0 = neutral
        rpn_bbox:       [N, (dy, dx, log(dh), log(dw))] Anchor bbox deltas.
    """
    # RPN Match: 1 = positive anchor, -1 = negative anchor, 0 = neutral
    rpn_match = np.zeros([anchors.shape[0]], dtype=np.int32)
    # RPN bounding boxes: [max anchors per image, (dy, dx, log(dh), log(dw))]
    rpn_bbox = np.zeros((config.RPN.TRAIN_ANCHORS_PER_IMAGE, 4))

    # Handle COCO crowds
    # A crowd box in COCO is a bounding box around several instances. Exclude
    # them from training. A crowd box is given a negative class ID.
    crowd_ix = np.where(gt_class_ids < 0)[0]
    if crowd_ix.shape[0] > 0:
        # Filter out crowds from ground truth class IDs and boxes
        non_crowd_ix = np.where(gt_class_ids > 0)[0]
        crowd_boxes = gt_boxes[crowd_ix]
        gt_class_ids = gt_class_ids[non_crowd_ix]
        gt_boxes = gt_boxes[non_crowd_ix]
        # Compute overlaps with crowd boxes [anchors, crowds]
        crowd_overlaps = bbox_overlaps(anchors, crowd_boxes)
        crowd_iou_max = np.amax(crowd_overlaps, axis=1)
        no_crowd_bool = (crowd_iou_max < 0.001)
    else:
        # All anchors don't intersect a crowd
        no_crowd_bool = np.ones([anchors.shape[0]], dtype=bool)

    # Compute overlaps [num_anchors, num_gt_boxes]
    # previously known as "compute_overlaps"
    overlaps = bbox_overlaps(anchors, gt_boxes)

    # Match anchors to GT Boxes
    # If an anchor overlaps a GT box with IoU >= 0.7 then it's positive.
    # If an anchor overlaps a GT box with IoU < 0.3 then it's negative.
    # Neutral anchors are those that don't match the conditions above,
    # and they don't influence the loss function.
    # However, don't keep any GT box unmatched (rare, but happens). Instead,
    # match it to the closest anchor (even if its max IoU is < 0.3).
    #
    # 1. Set negative anchors first. They get overwritten below if a GT box is
    # matched to them. Skip boxes in crowd areas.
    anchor_iou_argmax = np.argmax(overlaps, axis=1)
    anchor_iou_max = overlaps[np.arange(overlaps.shape[0]), anchor_iou_argmax]
    rpn_match[(anchor_iou_max < 0.3) & no_crowd_bool] = -1
    # 2. Set an anchor for each GT box (regardless of IoU value).
    # TODO: If multiple anchors have the same IoU match all of them
    gt_iou_argmax = np.argmax(overlaps, axis=0)
    rpn_match[gt_iou_argmax] = 1
    # 3. Set anchors with high overlap as positive.
    rpn_match[anchor_iou_max >= 0.7] = 1

    # Subsample to balance positive and negative anchors
    # Don't let positives be more than half the anchors
    ids = np.where(rpn_match == 1)[0]
    extra = len(ids) - (config.RPN.TRAIN_ANCHORS_PER_IMAGE // 2)
    if extra > 0:
        # Reset the extra ones to neutral
        ids = np.random.choice(ids, extra, replace=False)
        rpn_match[ids] = 0
    # Same for negative proposals
    ids = np.where(rpn_match == -1)[0]
    extra = len(ids) - (config.RPN.TRAIN_ANCHORS_PER_IMAGE - np.sum(rpn_match == 1))
    if extra > 0:
        # Rest the extra ones to neutral
        ids = np.random.choice(ids, extra, replace=False)
        rpn_match[ids] = 0

    # For positive anchors, compute shift and scale needed to transform them
    # to match the corresponding GT boxes.
    ids = np.where(rpn_match == 1)[0]
    ix = 0  # index into rpn_bbox
    # TODO (low): use box_refinement() rather than duplicating the code here
    for i, a in zip(ids, anchors[ids]):
        # Closest gt box (it might have IoU < 0.7)
        gt = gt_boxes[anchor_iou_argmax[i]]

        # Convert coordinates to center plus width/height.
        # GT Box
        gt_h = gt[2] - gt[0]
        gt_w = gt[3] - gt[1]
        gt_center_y = gt[0] + 0.5 * gt_h
        gt_center_x = gt[1] + 0.5 * gt_w
        # Anchor
        a_h = a[2] - a[0]
        a_w = a[3] - a[1]
        a_center_y = a[0] + 0.5 * a_h
        a_center_x = a[1] + 0.5 * a_w

        # Compute the bbox refinement that the RPN should predict.
        rpn_bbox[ix] = [
            (gt_center_y - a_center_y) / a_h,
            (gt_center_x - a_center_x) / a_w,
            np.log(gt_h / a_h),
            np.log(gt_w / a_w),
        ]
        # Normalize
        rpn_bbox[ix] /= config.DATA.BBOX_STD_DEV
        ix += 1

    return rpn_match, rpn_bbox


############################################################
#  Proposal Layer
############################################################
def proposal_layer(inputs, proposal_count, nms_threshold, priors, config=None):
    """Receives anchor scores and selects a subset to pass as proposals
    to the second stage. Filtering is done based on anchor scores and
    non-max suppression to remove overlaps. It also applies bounding
    box refinement details to anchors.

    Args:
        inputs
            [0] rpn_probs: [batch, anchors, (bg prob, fg prob)]
            [1] rpn_bbox: [batch, anchors, (dy, dx, log(dh), log(dw))]
        proposal_count
        nms_threshold
        anchors
        config

    Returns:
        Proposals in normalized coordinates [batch, rois, (y1, x1, y2, x2)]
    """
    anchors = Variable(priors.cuda(), requires_grad=False)
    bs, prior_num = inputs[0].size(0), anchors.size(0)
    # Box Scores. Use the foreground class confidence. [Batch, num_rois, 1]
    scores = inputs[0][:, :, 1]

    # Box deltas [batch, num_rois, 4]
    deltas = inputs[1]
    std_dev = Variable(torch.from_numpy(np.reshape(config.DATA.BBOX_STD_DEV, [1, 1, 4])).float(),
                       requires_grad=False).cuda()
    deltas = deltas * std_dev

    anchors = anchors.expand(bs, anchors.size(0), anchors.size(1))

    # Improve performance by trimming to top anchors by score
    # and doing the rest on the smaller subset.
    pre_nms_limit = min(6000, prior_num)
    scores, order = scores.sort(descending=True)
    scores = scores[:, :pre_nms_limit]
    order = order[:, :pre_nms_limit]

    deltas_trim = Variable(torch.FloatTensor(bs, pre_nms_limit, 4).cuda())
    anchors_trim = Variable(torch.FloatTensor(bs, pre_nms_limit, 4).cuda())
    # index two-dim (out_of_mem if directly index order.data)
    for i in range(bs):
        deltas_trim[i] = deltas[i][order.data[i], :]
        anchors_trim[i] = anchors[i][order.data[i], :]

    # Apply deltas to anchors to get refined anchors.
    # [batch, N, (y1, x1, y2, x2)]
    boxes = apply_box_deltas(anchors_trim, deltas_trim)       # TODO: nan or inf in initial iter

    # Clip to image boundaries. [batch, N, (y1, x1, y2, x2)]
    height, width = config.DATA.IMAGE_SHAPE[:2]
    window = np.array([0, 0, height, width]).astype(np.float32)
    window = Variable(torch.from_numpy(window).cuda(), requires_grad=False)
    boxes = clip_boxes(boxes, window)

    # Filter out small boxes
    # According to Xinlei Chen's paper, this reduces detection accuracy
    # for small objects, so we're skipping it.

    # Non-max suppression
    keep = nms(torch.cat((boxes, scores.unsqueeze(2)), 2).data, nms_threshold)
    keep = keep[:, :proposal_count]
    boxes_keep = Variable(torch.FloatTensor(bs, keep.shape[1], 4).cuda())  # bs, proposal_count(1000), 4
    for i in range(bs):
        boxes_keep[i] = boxes[i][keep[i], :]

    # Normalize dimensions to range of 0 to 1.
    norm = Variable(torch.from_numpy(np.array([height, width, height, width])).float(), requires_grad=False).cuda()
    normalized_boxes = boxes_keep / norm

    return normalized_boxes   # proposals


############################################################
#  ROIAlign Layer
############################################################
def pyramid_roi_align(inputs, pool_size, image_shape):
    """Implements ROI Pooling on multiple levels of the feature pyramid.
    Args:
        pool_size: [height, width] of the output pooled regions. Usually [7, 7]
        image_shape: [height, width, channels]. Shape of input image in pixels

        inputs:
            - boxes: [batch, num_boxes, (y1, x1, y2, x2)] in normalized coordinates.
            - Feature maps: List of feature maps from different levels of the pyramid.
                        Each is [batch, channels, height, width]
    Output:
        Pooled regions in the shape: [num_boxes, height, width, channels].
        The width and height are those specific in the pool_shape in the layer constructor.
    """

    # Crop boxes [batch, num_boxes, (y1, x1, y2, x2)] in normalized coordinates
    boxes = inputs[0]   # aka ROIs

    # Feature Maps. List of feature maps from different level of the
    # feature pyramid. Each is [batch, height, width, channels]
    feature_maps = inputs[1:]

    # Assign each ROI to a level in the pyramid based on the ROI area.
    y1, x1, y2, x2 = boxes.chunk(4, dim=2)
    h = y2 - y1
    w = x2 - x1

    # Equation 1 in the Feature Pyramid Networks paper. Account for
    # the fact that our coordinates are normalized here.
    # e.g. a 224x224 ROI (in pixels) maps to P4
    image_area = Variable(torch.FloatTensor([float(image_shape[0]*image_shape[1])]), requires_grad=False)
    if boxes.is_cuda:
        image_area = image_area.cuda()
    roi_level = 4 + utils.log2(torch.sqrt(h*w)/(224.0/torch.sqrt(image_area)))
    roi_level = roi_level.round().int()
    # in case batch size =1, we keep that dim
    roi_level = roi_level.clamp(2, 5).squeeze(dim=-1)   # size: [bs, num_roi], say [3, 1000 or 2000]

    # Loop through levels and apply ROI pooling to each. P2 to P5.
    pooled = []
    box_to_level = []
    for i, level in enumerate(range(2, 6)):
        ix = roi_level == level
        if not ix.any():
            continue
        index = torch.nonzero(ix)    # ix: bs, 1000; index: say, 2670 x 2
        level_boxes = boxes[index[:, 0].data, index[:, 1].data, :]    # from boxes: [bs, 1000, 4] -> [index[0], 4]

        # Keep track of which box is mapped to which level
        box_to_level.append(index.data)

        # Stop gradient propagation to ROI proposals (_rois is already detached)
        # level_boxes = level_boxes.detach()

        # Crop and Resize
        box_ind = index[:, 0].int()
        curr_feature_maps = feature_maps[i]
        pooled_features = CropAndResizeFunction(pool_size, pool_size)(curr_feature_maps, level_boxes, box_ind)
        pooled.append(pooled_features)

    # Pack pooled features into one tensor
    pooled = torch.cat(pooled, dim=0)
    # Pack box_to_level mapping into one array and add another
    # column representing the order of pooled boxes
    box_to_level = torch.cat(box_to_level, dim=0)

    # Rearrange pooled features to match the order of the original boxes
    # _, box_to_level = torch.sort(box_to_level)
    # pooled = pooled[box_to_level, :, :]

    pooled_out = Variable(torch.zeros(
        boxes.size(0), boxes.size(1), pooled.size(1), pooled.size(2), pooled.size(3)).cuda())
    pooled_out[box_to_level[:, 0], box_to_level[:, 1], :, :, :] = pooled
    # 3, 1000, 256, 7, 7 -> 3000, 256, 7, 7
    pooled_out = pooled_out.view(-1, pooled_out.size(2), pooled_out.size(3), pooled_out.size(4))

    return pooled_out


############################################################
#  Detection Target Layer (Train)
############################################################
def generate_roi(config, proposals, gt_class_ids, gt_boxes, gt_masks):
    # PER SAMPLE OPERATION
    # proposals: N, 4
    # gt_class_ids: size N

    if torch.nonzero(gt_class_ids < 0).size():

        _ind_crowd = torch.nonzero(gt_class_ids < 0).squeeze()
        _ind_non_crowd = torch.nonzero(gt_class_ids > 0).squeeze()
        crowd_boxes = gt_boxes[_ind_crowd, :]

        gt_class_ids = gt_class_ids[_ind_non_crowd]
        gt_boxes = gt_boxes[_ind_non_crowd, :]
        gt_masks = gt_masks[_ind_non_crowd, :, :]

        # Compute overlaps with crowd boxes [anchors, crowds]
        crowd_overlaps = bbox_overlaps(proposals, crowd_boxes)  # shape [N, num_crowd_boxes]
        crowd_iou_max = torch.max(crowd_overlaps, dim=-1)[0]
        no_crowd_bool = crowd_iou_max < 0.001
    else:
        no_crowd_bool = Variable(torch.ByteTensor(proposals.size(0)), requires_grad=False).cuda()
        no_crowd_bool[:] = True

    # Compute overlaps matrix [bs, proposals, gt_boxes]
    try:
        overlaps = bbox_overlaps(proposals, gt_boxes)   # TODO: gt_boxes might be empty
    except:
        print('proposals size: ', proposals.size())
        print('gt_boxes size: ', gt_boxes.size())

    # Determine positive and negative ROIs
    # shape [bs, N], means the maximum overlap for each RoI (N) with GTs
    roi_iou_max = torch.max(overlaps, dim=-1)[0]

    # Positive ROIs are those with >= 0.5 IoU with a GT box
    pos_roi_bool = roi_iou_max >= 0.5  # shape [bs, N]
    # Negative ROIs are those with < 0.5 with every GT box. Skip crowds.
    neg_roi_bool = roi_iou_max < 0.5
    neg_roi_bool = neg_roi_bool & no_crowd_bool

    # ============================================
    ROIS, ROI_GT_CLASS_IDS, DELTAS, MASKS = None, None, None, None

    if torch.nonzero(pos_roi_bool).size():
        pos_ind = torch.nonzero(pos_roi_bool)[:, 0]

        pos_cnt_per_im = int(config.ROIS.TRAIN_ROIS_PER_IMAGE*config.ROIS.ROI_POSITIVE_RATIO)
        rand_idx = torch.randperm(pos_ind.size(0)).cuda()
        rand_idx = rand_idx[:pos_cnt_per_im]
        pos_ind = pos_ind[rand_idx]
        pos_cnt = pos_ind.size(0)

        POS_ROIS = proposals[pos_ind.data, :]

        # ROI_GT_CLASS_IDS
        # Assign positive ROIs to GT boxes.
        pos_overlaps = overlaps[pos_ind.data, :]    # shape: pos_cnt, gt_num
        roi_gt_box_assignment = torch.max(pos_overlaps, dim=1)[1]
        roi_gt_boxes = gt_boxes[roi_gt_box_assignment, :]
        ROI_GT_CLASS_IDS = gt_class_ids[roi_gt_box_assignment].int()

        # DELTAS
        # Compute bbox refinement for positive ROIs
        DELTAS = Variable(box_refinement(POS_ROIS.data, roi_gt_boxes.data), requires_grad=False)
        std_dev = Variable(torch.from_numpy(config.DATA.BBOX_STD_DEV).float(), requires_grad=False)
        if config.MISC.GPU_COUNT:
            std_dev = std_dev.cuda()
        DELTAS /= std_dev

        # MASKS
        # Assign positive ROIs to GT masks
        try:
            roi_masks = gt_masks[roi_gt_box_assignment, :, :]  # shape: pos_cnt, mask_shape, mask_shape
        except:
            a = 1
        # Compute mask targets
        boxes = POS_ROIS
        if config.MRCNN.USE_MINI_MASK:
            # Transform ROI coordinates from normalized image space
            # to normalized mini-mask space.
            y1, x1, y2, x2 = POS_ROIS.chunk(4, dim=1)
            gt_y1, gt_x1, gt_y2, gt_x2 = roi_gt_boxes.chunk(4, dim=1)
            gt_h = gt_y2 - gt_y1
            gt_w = gt_x2 - gt_x1
            y1 = (y1 - gt_y1) / gt_h
            x1 = (x1 - gt_x1) / gt_w
            y2 = (y2 - gt_y1) / gt_h
            x2 = (x2 - gt_x1) / gt_w
            boxes = torch.cat([y1, x1, y2, x2], dim=1)

        box_ids = Variable(torch.arange(roi_masks.size(0)), requires_grad=False).cuda().int()
        masks = Variable(
            CropAndResizeFunction(config.MRCNN.MASK_SHAPE[0], config.MRCNN.MASK_SHAPE[1])
            (roi_masks.unsqueeze(1), boxes, box_ids).data,
            requires_grad=False)
        masks = masks.squeeze(1)
        # Threshold mask pixels at 0.5 to have GT masks be 0 or 1 to use with
        # binary cross entropy loss.
        MASKS = torch.round(masks)
    else:
        pos_cnt = 0

    # Negative ROIs. Add enough to maintain positive:negative ratio.
    if torch.nonzero(neg_roi_bool).size() and pos_cnt > 0:
        neg_ind = torch.nonzero(neg_roi_bool)[:, 0]
        r = 1.0 / config.ROIS.ROI_POSITIVE_RATIO
        neg_cnt = int(r * pos_cnt - pos_cnt)
        rand_idx = torch.randperm(neg_ind.size(0))
        rand_idx = rand_idx[:neg_cnt]
        if config.MISC.GPU_COUNT:
            rand_idx = rand_idx.cuda()
        neg_ind = neg_ind[rand_idx]
        neg_cnt = neg_ind.size(0)
        NEG_ROIS = proposals[neg_ind, :]
    else:
        neg_cnt = 0

    # Append negative ROIs and pad bbox deltas and masks that
    # are not used for negative ROIs with zeros.
    if pos_cnt > 0 and neg_cnt > 0:

        ROIS = torch.cat((POS_ROIS, NEG_ROIS), dim=0)

        zeros = Variable(torch.zeros(neg_cnt).cuda(), requires_grad=False).int()
        ROI_GT_CLASS_IDS = torch.cat([ROI_GT_CLASS_IDS, zeros], dim=0)

        zeros = Variable(torch.zeros(neg_cnt, 4).cuda(), requires_grad=False)
        DELTAS = torch.cat([DELTAS, zeros], dim=0)

        zeros = Variable(torch.zeros(neg_cnt, config.MRCNN.MASK_SHAPE[0], config.MRCNN.MASK_SHAPE[1]).cuda(),
                         requires_grad=False)
        MASKS = torch.cat([MASKS, zeros], dim=0)

    elif pos_cnt > 0:

        ROIS = POS_ROIS

    elif neg_cnt > 0:

        ROIS = NEG_ROIS

        zeros = Variable(torch.zeros(neg_cnt).cuda(), requires_grad=False).int()
        ROI_GT_CLASS_IDS = zeros

        zeros = Variable(torch.zeros(neg_cnt, 4).cuda(), requires_grad=False)
        DELTAS = zeros

        zeros = Variable(torch.zeros(neg_cnt, config.MRCNN.MASK_SHAPE[0], config.MRCNN.MASK_SHAPE[1]).cuda(),
                         requires_grad=False)
        MASKS = zeros

    # # updated: pad ROIS
    # if ROIS.size(0) < config.TRAIN_ROIS_PER_IMAGE:
    #     more_zero_num = config.TRAIN_ROIS_PER_IMAGE - ROIS.size(0)
    #     zeros = Variable(torch.zeros(more_zero_num, 4).cuda())  # should require gradient
    #     ROIS = torch.cat((ROIS, zeros), dim=0)

    return ROIS, ROI_GT_CLASS_IDS, DELTAS, MASKS


def prepare_det_target(proposals, gt_class_ids, gt_boxes, gt_masks, config):
    """Sub-samples proposals and generates target box refinement, class_ids and masks.
        Note that proposal class IDs, gt_boxes, and gt_masks are zero padded.
        Equally, returned rois and targets are zero padded.

    Args:
        proposals:          [batch, N, (y1, x1, y2, x2)] in normalized coordinates.
                                Might be zero padded if there are not enough proposals.
        gt_class_ids:       [batch, MAX_GT_NUM] Integer class IDs.
        gt_boxes:           [batch, MAX_GT_NUM, (y1, x1, y2, x2)] in normalized coordinates.
        gt_masks:           [batch, MAX_GT_NUM, height (or smaller), width] of boolean type (might be mini-masked)
        config:             configuration

    Notes:
        MAX_GT_NUM <= config.MAX_GT_INSTANCES: it's the max_gt_num within this batch

    Returns:
        rois:               [batch, TRAIN_ROIS_PER_IMAGE, (y1, x1, y2, x2)] in normalized coordinates
        target_class_ids:   [batch, TRAIN_ROIS_PER_IMAGE]. Integer class IDs.
        target_deltas:      [batch, TRAIN_ROIS_PER_IMAGE, NUM_CLASSES, (dy, dx, log(dh), log(dw), class_id)]
                                Class-specific bbox refinements.
        target_mask:        [batch, TRAIN_ROIS_PER_IMAGE, height (exactly MASK_SHAPE), width)
                                Masks cropped to bbox boundaries and resized to neural network output size.
        volatiles:          prepare empty output if rois_out is all zeros.
    """
    bs = proposals.size(0)
    # set up new variables
    num_rois = config.ROIS.TRAIN_ROIS_PER_IMAGE   # max_rois_per_image
    mask_sz = config.MRCNN.MASK_SHAPE[0]

    rois_out = Variable(torch.zeros(bs, num_rois, 4).cuda())
    # rois_out = []
    target_class_ids = Variable(torch.IntTensor(bs, num_rois).zero_().cuda(), requires_grad=False)
    target_deltas = Variable(torch.zeros(bs, num_rois, 4).cuda(), requires_grad=False)
    target_mask = Variable(torch.zeros(bs, num_rois, mask_sz, mask_sz).cuda(), requires_grad=False)

    for i in range(bs):
        # per sample
        rois, roi_gt_class_ids, deltas, masks = \
            generate_roi(config, proposals[i], gt_class_ids[i], gt_boxes[i], gt_masks[i])
        if rois is not None:
            curr_rois_num = rois.size(0)
            # print('curr_rois_num: ', curr_rois_num)
            # print('roi_gt_class_ids: ', roi_gt_class_ids.size())
            rois_out[i, :curr_rois_num] = rois
            # rois_out.append(rois)
            target_class_ids[i, :curr_rois_num] = roi_gt_class_ids
            target_deltas[i, :curr_rois_num] = deltas
            target_mask[i, :curr_rois_num] = masks

    return rois_out, target_class_ids, target_deltas, target_mask


############################################################
#  Detection Layer (for evaluation)
############################################################
def conduct_nms(class_ids, refined_rois, class_scores, keep, config):
    """per SAMPLE operation; no batch size dim!
    Args:
        class_ids       [say 1000]
        refined_rois    [1000 4]
        class_scores    [1000]
        keep            [True, False, ...] altogether 1000
        config
    Returns:
        detection:      [DET_MAX_INSTANCES, (y1, x1, y2, x2, class_id, class_score)]
    """
    pre_nms_class_ids = class_ids[keep]
    pre_nms_scores = class_scores[keep]
    pre_nms_rois = refined_rois[torch.nonzero(keep).squeeze(), :]
    _indx = torch.nonzero(keep).squeeze()

    # conduct nms per CLASS
    for i, class_id in enumerate(utils.unique1d(pre_nms_class_ids)):

        # Pick detections of this class
        ixs = torch.nonzero(class_id == pre_nms_class_ids).squeeze()

        ix_scores = pre_nms_scores[ixs]
        ix_rois = pre_nms_rois[ixs, :]

        # Sort
        ix_scores, order = ix_scores.sort(descending=True)
        ix_rois = ix_rois[order, :]

        class_keep = nms(torch.cat((ix_rois, ix_scores.unsqueeze(1)), dim=1).unsqueeze(0).data,
                         config.TEST.DET_NMS_THRESHOLD)[0]

        # Map indices
        class_keep = _indx[ixs[order[class_keep.tolist()]]]  # TODO (low): why not order[class_keep] directly?

        if i == 0:
            nms_keep = class_keep
        else:
            nms_keep = utils.unique1d(torch.cat((nms_keep, class_keep)))

    nms_indx = utils.intersect1d(_indx, nms_keep)

    # Keep top detections
    roi_count = config.TEST.DET_MAX_INSTANCES
    top_ids = class_scores[nms_indx].sort(descending=True)[1][:roi_count]
    final_index = nms_indx[top_ids].squeeze()

    # Arrange output as [DET_MAX_INSTANCES, (y1, x1, y2, x2, class_id, score)]
    # Coordinates are in image domain.
    detections = torch.cat((refined_rois[final_index],
                            class_ids[final_index].unsqueeze(1).float(),
                            class_scores[final_index].unsqueeze(1)), dim=1)
    return detections


def detection_layer(rois, probs, deltas, image_meta, config):
    """Takes classified proposal boxes and their bounding box deltas and
    returns the final detection boxes.

    Args:
        config
        rois:                   [bs, 1000 (just an example), 4 (y1, x1, y2, x2)], in normalized coordinates
        probs (mrcnn_class):    [bs*1000, 81]
        deltas (mrcnn_bbox):    [bs*1000, 81, 4], (dy, dx, log(dh), log(dw))
        image_meta:             [bs, 89] Variable
    Returns:
        detections:             [batch, num_detections, (y1, x1, y2, x2, class_id, class_score)]
    """
    bs = rois.size(0)
    box_num_per_sample = rois.size(1)
    detections = Variable(torch.zeros(bs, config.TEST.DET_MAX_INSTANCES, 6).cuda(), volatile=True)

    # windows: (y1, x1, y2, x2) in image coordinates.
    # The part of the image that contains the image excluding the padding.
    _, _, windows, _ = parse_image_meta(image_meta)
    # Class IDs per ROI
    class_scores, class_ids = torch.max(probs, dim=1)

    # Class probability of the top class of each ROI
    # Class-specific bounding box deltas
    _idx = torch.arange(class_ids.size(0)).cuda().long()
    deltas_specific = deltas[_idx, class_ids]   # TODO (important): good example of 2D index

    # Apply bounding box deltas
    # Shape: [boxes, (y1, x1, y2, x2)] in normalized coordinates
    std_dev = Variable(torch.from_numpy(np.reshape(config.DATA.BBOX_STD_DEV, [1, 4])).float(), requires_grad=False)
    if config.MISC.GPU_COUNT:
        std_dev = std_dev.cuda()
    deltas_specific *= std_dev

    rois = rois.view(-1, 4)
    refined_rois = apply_box_deltas(rois.unsqueeze(0), deltas_specific.unsqueeze(0))
    # Convert coordinates to image domain
    height, width = config.DATA.IMAGE_SHAPE[:2]
    scale = Variable(torch.from_numpy(np.array([height, width, height, width])).float(), requires_grad=False)
    if config.MISC.GPU_COUNT:
        scale = scale.cuda()
    refined_rois *= scale
    # Clip boxes to image window
    refined_rois = clip_boxes(refined_rois, windows)
    # Round and cast to int since we're dealing with pixels now
    refined_rois = torch.round(refined_rois)

    # Filter out background boxes, low confidence boxes and zero area boxes
    box_area = (refined_rois[:, 0] - refined_rois[:, 2])*(refined_rois[:, 1] - refined_rois[:, 3])
    keep_bool = (class_ids > 0) & (class_scores >= config.TEST.DET_MIN_CONFIDENCE) & (box_area > 0)

    if torch.nonzero(keep_bool).dim() == 0:
        # indicate no detected boxes!
        return detections

    # conduct nms per sample
    for i in range(bs):
        curr_start = i*box_num_per_sample
        curr_end = i*box_num_per_sample + box_num_per_sample
        curr_keep_bool = keep_bool[curr_start:curr_end]
        if torch.sum(curr_keep_bool.long()).data[0] == 0:
            continue
        curr_dets = conduct_nms(class_ids[curr_start:curr_end],
                                refined_rois[curr_start:curr_end, :],
                                class_scores[curr_start:curr_end],
                                curr_keep_bool,
                                config)
        actual_dets_num = curr_dets.size(0)
        detections[i, :actual_dets_num, :] = curr_dets

    return detections


############################################################
#  Loss Functions
############################################################
def compute_rpn_class_loss(rpn_match, rpn_class_logits):
    """RPN anchor classifier loss.
    rpn_match:          [batch, anchors, 1]. Anchor match type. 1=positive,-1=negative, 0=neutral anchor.
    rpn_class_logits:   [batch, anchors, 2]. RPN classifier logits for FG/BG.
    """
    # Squeeze last dim to simplify
    rpn_match = rpn_match.squeeze(-1)
    # Get anchor classes. Convert the -1/+1 match to 0/1 values.
    anchor_class = (rpn_match == 1).long()
    # Positive and Negative anchors contribute to the loss,
    # but neutral anchors (match value = 0) don't.
    indices = torch.nonzero(rpn_match != 0)

    # Pick rows that contribute to the loss and filter out the rest.
    rpn_class_logits = rpn_class_logits[indices.data[:, 0], indices.data[:, 1], :]
    anchor_class = anchor_class[indices.data[:, 0], indices.data[:, 1]]

    # Cross entropy loss
    loss = F.cross_entropy(rpn_class_logits, anchor_class)

    return loss


def compute_rpn_bbox_loss(target_bbox, rpn_match, rpn_bbox):
    """Return the RPN bounding box loss graph.

    target_bbox:    [batch, max_positive_anchors (say 256), (dy, dx, log(dh), log(dw))].
                        Uses 0 padding to fill in unused bbox deltas. shape, 6, 256, 4
    rpn_match:      [batch, anchors, 1]. Anchor match type. 1=positive,-1=negative, 0=neutral anchor.
    rpn_bbox:       [batch, anchors, (dy, dx, log(dh), log(dw))]. shape, 6, 25576, 4
    """

    # Squeeze last dim to simplify
    rpn_match = rpn_match.squeeze(2)
    # Positive anchors contribute to the loss, but negative and
    # neutral anchors (match value of 0 or -1) don't.
    indices = torch.nonzero(rpn_match == 1)
    # Pick bbox deltas that contribute to the loss
    rpn_bbox = rpn_bbox[indices.data[:, 0], indices.data[:, 1]]  # shape: say 27, 4; lose the batch dim info

    # Trim target bounding box deltas to the same length as rpn_bbox.
    bs = target_bbox.size(0)
    target_bbox_sort = Variable(torch.zeros(rpn_bbox.size()).cuda(), requires_grad=False)
    cnt = 0
    for i in range(bs):
        curr_size = sum(indices.data[:, 0] == i)
        target_bbox_sort[cnt:curr_size+cnt, :] = target_bbox[i, :curr_size, :]
        cnt += curr_size
    # Smooth L1 loss
    loss = F.smooth_l1_loss(rpn_bbox, target_bbox_sort)

    return loss


def compute_mrcnn_class_loss(target_class_ids, pred_class_logits):
    """Loss for the classifier head of Mask RCNN.

    target_class_ids:   [batch, num_rois]. Integer class IDs. Uses zero padding to fill in the array.
    pred_class_logits:  [batch, num_rois, num_classes]
    """
    if torch.sum(target_class_ids).data[0] != 0:
        loss = F.cross_entropy(pred_class_logits.view(-1, pred_class_logits.size(2)),
                               target_class_ids.long().view(-1))
    else:
        loss = Variable(torch.FloatTensor([0]), requires_grad=False)
        if target_class_ids.is_cuda:
            loss = loss.cuda()
    return loss


def compute_mrcnn_bbox_loss(target_bbox, target_class_ids, pred_bbox):
    """Loss for Mask R-CNN bounding box refinement.

    target_bbox:        [batch, num_rois, (dy, dx, log(dh), log(dw))]
    target_class_ids:   [batch, num_rois]. Integer class IDs.
    pred_bbox:          [batch, num_rois, num_classes, (dy, dx, log(dh), log(dw))]
    """

    if torch.sum(target_class_ids).data[0] != 0:
        # # Only positive ROIs contribute to the loss. And only
        # # the right class_id of each ROI. Get their indices.
        # TODO: optimize here, loss
        # in my ugly manner
        ugly_ind = torch.nonzero(target_class_ids > 0).long()
        target_bbox_sort = Variable(torch.zeros(ugly_ind.size(0), 4).cuda(), requires_grad=False)
        temp = Variable(torch.zeros(ugly_ind.size(0), 4).cuda(), requires_grad=True)
        pred_bbox_sort = temp.clone()

        for i in range(ugly_ind.size(0)):
            target_bbox_sort[i, :] = target_bbox[ugly_ind[i, 0], ugly_ind[i, 1], :]
            curr_cls = target_class_ids[ugly_ind[i, 0], ugly_ind[i, 1]].long()
            pred_bbox_sort[i, :] = pred_bbox[ugly_ind[i, 0], ugly_ind[i, 1], curr_cls, :]
        # Smooth L1 loss
        loss = F.smooth_l1_loss(pred_bbox_sort, target_bbox_sort)
    else:
        loss = Variable(torch.FloatTensor([0]), requires_grad=False)
        if target_class_ids.is_cuda:
            loss = loss.cuda()
    return loss


def compute_mrcnn_mask_loss(target_masks, target_class_ids, pred_masks):
    """Mask binary cross-entropy loss for the masks head.

    target_masks:       [batch, num_rois, height, width].
                            A float32 tensor of values 0 or 1. Uses zero padding to fill array.
    target_class_ids:   [batch, num_rois]. Integer class IDs. Zero padded.
    pred_masks:         [batch, proposals, height, width, num_classes] float32 tensor with values from 0 to 1.
    """
    if torch.sum(target_class_ids).data[0] != 0:
        # # Only positive ROIs contribute to the loss. And only
        # # the class specific mask of each ROI.
        # in my ugly manner
        mask_sz = target_masks.size(2)
        ugly_ind = torch.nonzero(target_class_ids > 0).long()
        y_true_sort = Variable(torch.zeros(ugly_ind.size(0), mask_sz, mask_sz).cuda(), requires_grad=False)
        temp = Variable(torch.zeros(y_true_sort.size()).cuda(), requires_grad=True)
        y_pred_sort = temp.clone()

        for i in range(ugly_ind.size(0)):
            y_true_sort[i, :, :] = target_masks[ugly_ind[i, 0], ugly_ind[i, 1], :, :]
            curr_cls = target_class_ids[ugly_ind[i, 0], ugly_ind[i, 1]].long()
            y_pred_sort[i, :, :] = pred_masks[ugly_ind[i, 0], ugly_ind[i, 1], curr_cls, :, :]

        # Binary cross entropy
        loss = F.binary_cross_entropy(y_pred_sort, y_true_sort)
    else:
        loss = Variable(torch.FloatTensor([0]), requires_grad=False)
        if target_class_ids.is_cuda:
            loss = loss.cuda()
    return loss


