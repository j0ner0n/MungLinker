#!/usr/bin/env python
"""This is a script that..."""
from __future__ import print_function, unicode_literals

import torch, cv2
from torch.autograd import Variable
from torch.nn import BCEWithLogitsLoss, MSELoss, BCELoss
from torch.optim import Adam, RMSprop
from torch.nn.modules.loss import _Loss, _WeightedLoss, _assert_no_grad

import argparse
import collections
import datetime
import logging
import os
import pprint
import random
import sys
import time

import numpy
from scipy.misc import imsave

# from munglinker.detector import ConnectedComponentDetector
# from munglinker.evaluation import evaluate_detection, compute_eval_metrics_multichannel, dice
# from munglinker.preprocessing import MUSCIMALabelsDataset
# from munglinker.preprocessing import MUSCIMALabelIterator
from munglinker.model import PyTorchTrainingStrategy, PyTorchNetwork
# from munglinker.augmentation import ImageAugmentationProcessor
from munglinker.utils import BColors, build_experiment_name, select_model
from munglinker.data_pool import load_munglinker_data

__version__ = "0.0.1"
__author__ = "Jan Hajic jr."


##############################################################################


def build_argument_parser():
    parser = argparse.ArgumentParser(description=__doc__, add_help=True,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('-m', '--model', required=True,
                        help='The name of the model that you wish to use.'
                             ' Has to be a name in the models/ subdir'
                             ' of munglinker (without the .py extension).')
    parser.add_argument('--load_params',
                        help='The state dict that should be loaded to initialize'
                             ' this model. Careful: in order to continue training,'
                             ' you would also have to recover the optimizer state.')

    parser.add_argument('-r', '--mung_root', action='store', required=True,
                        help='The root directory that contains the MuNG XMLs.')
    parser.add_argument('-i', '--image_root', action='store', required=True,
                        help='The root directory that contains the images of'
                             ' scores that are represented by the MuNGs. The'
                             ' image names must correspond to the MuNG file'
                             ' names, up to the file type suffix.')
    parser.add_argument('-s', '--split_file', action='store', required=True,
                        help='The split file that specifies which MUSCIMA++ items'
                             ' are training, validation, and test data.')

    parser.add_argument('-c', '--config_file', action='store', required=True,
                        help='The config file that specifies things like'
                             ' preprocessing.')

    # parser.add_argument('--validation_size', type=int, default=20,
    #                     action='store',
    #                     help='Number of images to use for validation.'
    #                          ' If set to 0, will validate on training data.'
    #                          ' (Useful for tiny datasets.)')
    # parser.add_argument('--validation_detection_threshold', type=float,
    #                     default=0.5, action='store',
    #                     help='Detector threshold for validation runs, '
    #                          'to record detection scores as well as dice.')

    parser.add_argument('-e', '--export', action='store', required=True,
                        help='Export the model params into this file.')

    parser.add_argument('-b', '--batch_size', type=int, default=2,
                        help='Minibatch size for training.')
    parser.add_argument('--n_epochs', type=int, default=100,
                        help='Number of training epochs.')
    parser.add_argument('--no_early_stopping', action='store_true',
                        help='Do not apply early-stopping, run until --n_epochs'
                             ' are exhausted.')
    parser.add_argument('--patience', type=int, default=50,
                        help='Number of steps without improvement in validation'
                             ' loss after which the learning rate is attenuated.')

    parser.add_argument('--n_epochs_per_checkpoint', type=int, default=100,
                        help='Make a checkpoint of the model every N epochs.'
                             ' The checkpoint goes under the same name as -e.')
    parser.add_argument('--continue_training', action='store_true',
                        help='If set, checks whether a model under the name set'
                             ' in -e already exists. If it does, initialize training'
                             ' using its parameters.')
    parser.add_argument('--validation_outputs_dump_root', action='store', default=None,
                        help=' Dump validation result images'
                             '(prob. masks, prob. maps and predicted labels) into this directory'
                             'plus the ``name`` (so that the dump root can be shared between'
                             'strategies and the images do not mix up).')

    parser.add_argument('-a', '--augmentation', action='store_true',
                        help='If set, will train with data augmentation:'
                             ' scaling magnitude 0.4, rotation 0.2,'
                             ' vertical dilation 4, horizontal dilation 1')

    parser.add_argument('--exp_tag', action='store',
                        help='Give the experiment some additional name.')

    parser.add_argument('--tb_log_dir', action='store',
                        help='Tensoroboard logs directory.')
    parser.add_argument('--show_architecture', action='store_true',
                        help='Print network architecture before training starts.')

    parser.add_argument('--debugging_regime', action='store_true',
                        help='If set, will severely limit the amount of training'
                             ' data in order to run through the whole operation'
                             ' as quickly as possible.')

    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Turn on INFO messages.')
    parser.add_argument('--debug', action='store_true',
                        help='Turn on DEBUG messages.')

    return parser


def main(args):
    logging.info('Starting main...')
    _start_time = time.clock()

    # ------------------------------------------------------------------------
    # Initializing the model

    use_cuda = torch.cuda.is_available()

    model_mod = select_model(args.model)
    build_model_fn = model_mod.get_build_model()
    net = build_model_fn()

    if args.load_params:
        logging.info('Attempting to initialize from state dict:'
                     ' {0}'.format(args.load_params))
        if os.path.isfile(args.load_params):
            try:
                state_dict = torch.load(args.load_params)
                net.load_state_dict(state_dict)
            except OSError as e:
                logging.warning('Attempting to load non-param file!')

    # ------------------------------------------------------------------------
    # Initializing the data

    data = load_munglinker_data(
        mung_root=args.mung_root,
        images_root=args.image_root,
        split_file=args.split_file,
        config_file=args.config_file,
        test_only=False,
        no_test=True)
    logging.info('Loaded pools; training data has {} entities'
                 ''.format(len(data['train'].train_entities)))

    # Iterators
    train_batch_iter = model_mod.train_batch_iterator(model_mod.BATCH_SIZE)
    valid_batch_iter = model_mod.valid_batch_iterator(model_mod.BATCH_SIZE)
    test_batch_iter = model_mod.test_batch_iterator(model_mod.BATCH_SIZE)
    # runtime_batch_iter = model_mod.runtime_batch_iterator()
    batch_iters = {'train': train_batch_iter,
                   'valid': valid_batch_iter,
                   'test': test_batch_iter}

    logging.info('Data initialized.')

    # ------------------------------------------------------------------------
    # Initializing the training strategy

    loss_fn_cls = BCELoss
    loss_fn_kwargs = dict()
    # if args.focal_loss:
    #     loss_fn_cls = FocalLossElemwise
    #     loss_fn_kwargs = {'gamma': args.focal_loss_gamma}
    #     # The focal loss needs to have sigmoid applied to it already!
    #     model.net.apply_sigmoid = True

    exp_name = build_experiment_name(args)
    strategy = PyTorchTrainingStrategy(name=exp_name,
                                       loss_fn_class=loss_fn_cls,
                                       loss_fn_kwargs=loss_fn_kwargs,
                                       n_epochs_per_checkpoint=args.n_epochs_per_checkpoint,
                                       validation_use_detector=True,
                                       best_model_by_fscore=False,
                                       max_epochs=args.n_epochs,
                                       validation_subsample_window=None,
                                       validation_stride_ratio=None,
                                       validation_nodetector_subsample_window=None,
                                       validation_outputs_dump_root=args.validation_outputs_dump_root,
                                       checkpoint_export_file=args.export,
                                       best_params_file=args.export,
                                       improvement_patience=args.patience)
    if args.no_early_stopping:
        strategy.improvement_patience = args.n_epochs + 1
        strategy.early_stopping = False

    model = PyTorchNetwork(net=net, print_architecture=True)

    # ------------------------------------------------------------------------
    # Run training.

    print('Fitting model...')
    model.fit(data=data,
              batch_iters=batch_iters,
              training_strategy=strategy,
              dump_file=None,
              log_file=None,
              tensorboard_log_path=args.tb_log_dir)

    print('Saving model to: {0}'.format(args.export))
    torch.save(net.state_dict(), args.export)

    _end_time = time.clock()
    logging.info('train.py done in {0:.3f} s'.format(_end_time - _start_time))


if __name__ == '__main__':
    parser = build_argument_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
    if args.debug:
        logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.DEBUG)

    main(args)
