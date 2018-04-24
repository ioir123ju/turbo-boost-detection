import math
import numpy as np
import os
import datetime
from tools.utils import print_log


# Base Configuration Class
# Don't use this class directly. Instead, sub-class it and override
# the configurations you need to change.
class Config(object):
    """Base configuration class. For custom configurations, create a
    sub-class that inherits from this one and override properties
    that need to be changed.
    """

    # Path to pretrained imagenet model # TODO: loading is buggy
    PRETRAIN_IMAGENET_MODEL_PATH = os.path.join(os.getcwd(), 'datasets/pretrain_model', "resnet50_imagenet.pth")
    # Path to pretrained weights file
    PRETRAIN_COCO_MODEL_PATH = os.path.join(os.getcwd(), 'datasets/pretrain_model', 'mask_rcnn_coco.pth')
    MODEL_FILE_CHOICE = 'last'  # or file (xxx.pth)

    # NUMBER OF GPUs to use. For CPU use 0
    GPU_COUNT = 1

    # Number of images to train with on each GPU. A 12GB GPU can typically
    # handle 2 images of 1024x1024px.
    # Adjust based on your GPU memory and image sizes. Use the highest
    # number that your GPU can handle for best performance.
    IMAGES_PER_GPU = 1

    # Number of training steps per epoch
    # This doesn't need to match the size of the training set. Tensorboard
    # updates are saved at the end of each epoch, so setting this to a
    # smaller number means getting more frequent TensorBoard updates.
    # Validation stats are also calculated at each epoch end and they
    # might take a while, so don't set this too small to avoid spending
    # a lot of time on validation stats.
    STEPS_PER_EPOCH = 1000

    # TODO: deprecated already
    # Number of validation steps to run at the end of every training epoch.
    # A bigger number improves accuracy of validation stats, but slows
    # down the training.
    # VALIDATION_STEPS = 50

    # The strides of each layer of the FPN Pyramid. These values
    # are based on a Resnet101 backbone.
    BACKBONE_STRIDES = [4, 8, 16, 32, 64]

    # Number of classification classes (including background)
    NUM_CLASSES = 1  # Override in sub-classes

    # Length of square anchor side in pixels
    RPN_ANCHOR_SCALES = (32, 64, 128, 256, 512)

    # Ratios of anchors at each cell (width/height)
    # A value of 1 represents a square anchor, and 0.5 is a wide anchor
    RPN_ANCHOR_RATIOS = [0.5, 1, 2]

    # Anchor stride
    # If 1 then anchors are created for each cell in the backbone feature map.
    # If 2, then anchors are created for every other cell, and so on.
    RPN_ANCHOR_STRIDE = 1

    # Non-max suppression threshold to filter RPN proposals.
    # You can reduce this during training to generate more propsals.
    RPN_NMS_THRESHOLD = 0.7

    # How many anchors per image to use for RPN training
    RPN_TRAIN_ANCHORS_PER_IMAGE = 256

    # ROIs kept after non-maximum supression (training and inference)
    POST_NMS_ROIS_TRAINING = 2000
    POST_NMS_ROIS_INFERENCE = 1000

    # If enabled, resize instance masks to a smaller size to reduce
    # memory load. Recommended when using high-resolution images.
    USE_MINI_MASK = True
    MINI_MASK_SHAPE = (56, 56)  # (height, width) of the mini-mask

    # Input image resize
    # Images are resized such that the smallest side is >= IMAGE_MIN_DIM and
    # the longest side is <= IMAGE_MAX_DIM. In case both conditions can't
    # be satisfied together the IMAGE_MAX_DIM is enforced.
    IMAGE_MIN_DIM = 800
    IMAGE_MAX_DIM = 1024
    # If True, pad images with zeros such that they're (max_dim by max_dim)
    IMAGE_PADDING = True  # currently, the False option is not supported

    # Image mean (RGB)
    MEAN_PIXEL = np.array([123.7, 116.8, 103.9])

    # Number of ROIs per image to feed to classifier/mask heads
    # The Mask RCNN paper uses 512 but often the RPN doesn't generate
    # enough positive proposals to fill this and keep a positive:negative
    # ratio of 1:3. You can increase the number of proposals by adjusting
    # the RPN NMS threshold.
    TRAIN_ROIS_PER_IMAGE = 200

    # Percent of positive ROIs used to train classifier/mask heads
    ROI_POSITIVE_RATIO = 0.33

    # Pooled ROIs
    POOL_SIZE = 7
    MASK_POOL_SIZE = 14
    MASK_SHAPE = [28, 28]

    # Maximum number of ground truth instances to use in one image
    MAX_GT_INSTANCES = 100

    # Bounding box refinement standard deviation for RPN and final detections.
    RPN_BBOX_STD_DEV = np.array([0.1, 0.1, 0.2, 0.2])
    BBOX_STD_DEV = np.array([0.1, 0.1, 0.2, 0.2])

    # Max number of final detections
    DETECTION_MAX_INSTANCES = 100

    # Minimum probability value to accept a detected instance
    # ROIs below this threshold are skipped
    DETECTION_MIN_CONFIDENCE = 0.7

    # Non-maximum suppression threshold for detection
    DETECTION_NMS_THRESHOLD = 0.3

    # Learning rate and momentum
    # The Mask RCNN paper uses lr=0.02, but on TensorFlow it causes
    # weights to explode. Likely due to differences in optimzer
    # implementation.
    LEARNING_RATE = 0.001
    LEARNING_MOMENTUM = 0.9
    # Weight decay regularization
    WEIGHT_DECAY = 0.0001

    # Use RPN ROIs or externally generated ROIs for training
    # Keep this True for most situations. Set to False if you want to train
    # the head branches on ROI generated by code rather than the ROIs from
    # the RPN. For example, to debug the classifier head without having to
    # train the RPN.
    USE_RPN_ROIS = True

    SHOW_INTERVAL = 200
    SAVE_TIME_WITHIN_EPOCH = 10
    USE_VISDOM = False

    def _set_value(self):
        """Set values of computed attributes."""
        # Effective batch size
        if hasattr(self, 'BATCH_SIZE'):
            print('use the new BATCH_SIZE scheme!')
            self.old_scheme = False
        else:
            print('use the old BATCH_SIZE scheme!')
            # TODO: will be deprecated forever
            if self.GPU_COUNT > 0:
                self.BATCH_SIZE = self.IMAGES_PER_GPU * self.GPU_COUNT
            else:
                self.BATCH_SIZE = self.IMAGES_PER_GPU
            # Adjust step size based on batch size
            self.STEPS_PER_EPOCH *= self.BATCH_SIZE
            self.old_scheme = True

        # Input image size
        self.IMAGE_SHAPE = np.array(
            [self.IMAGE_MAX_DIM, self.IMAGE_MAX_DIM, 3])

        # Compute backbone size from input image size
        self.BACKBONE_SHAPES = np.array(
            [[int(math.ceil(self.IMAGE_SHAPE[0] / stride)),
              int(math.ceil(self.IMAGE_SHAPE[1] / stride))]
             for stride in self.BACKBONE_STRIDES])

        if self.DEBUG:
            self.SHOW_INTERVAL = 1

    def display(self, log_file):
        """Display Configuration values."""
        now = datetime.datetime.now()
        print_log('start timestamp: {:%Y%m%dT%H%M}'.format(now), file=log_file, init=True)
        print_log("\nConfigurations:", file=log_file)
        for a in dir(self):
            if not a.startswith("__") and not callable(getattr(self, a)):
                print_log("{:30} {}".format(a, getattr(self, a)), log_file)
        print_log("\n", log_file)


class CocoConfig(Config):
    """Configuration for training on MS COCO.
    Derives from the base Config class and overrides values specific
    to the COCO dataset.
    """

    # Number of classes (including background)
    NUM_CLASSES = 1 + 80  # COCO has 80 classes

    def __init__(self, config_name, args):
        super(CocoConfig, self).__init__()

        self.PHASE = args.phase
        self.DEBUG = args.debug
        self.DEVICE_ID = [int(x) for x in args.device_id.split(',')]
        self.GPU_COUNT = len(self.DEVICE_ID)
        self.NAME = config_name

        if self.PHASE == 'inference':
            self.DETECTION_MIN_CONFIDENCE = 0

        if self.NAME == 'hyli_default' or \
                        self.NAME == 'hyli_default_old':
            self.IMAGES_PER_GPU = 16
            # self.GPU_COUNT = 1

        elif self.NAME == 'all_new':
            self.BATCH_SIZE = 6
            self.MODEL_FILE_CHOICE = 'coco_pretrain'
            self.IMAGE_MIN_DIM = 256
            self.IMAGE_MAX_DIM = 320
            # self.USE_MINI_MASK = False
            # self.MINI_MASK_SHAPE = (28, 28)
            # self.DETECTION_NMS_THRESHOLD = 0.3

        elif self.NAME == 'all_new_2':
            self.BATCH_SIZE = 8
            self.MODEL_FILE_CHOICE = 'coco_pretrain'
        else:
            print('WARNING: unknown config name!!! use default setting.')
            # raise NameError('unknown config name!!!')

        self._set_value()