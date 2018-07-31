# /usr/bin/python
# -*- coding: utf-8 -*-

import tensorflow as tf

from tensorlayer.layers.core import Layer
from tensorlayer.layers.core import LayersConfig

from tensorlayer.layers.utils.quantization import bias_fold
from tensorlayer.layers.utils.quantization import w_fold
from tensorlayer.layers.utils.quantization import quantize_weight_overflow
from tensorlayer.layers.utils.quantization import quantize_weight_overflow

from tensorflow.python.training import moving_averages
from tensorlayer import logging

from tensorlayer.decorators import deprecated_alias

__all__ = ['QuantizedConv2dWithBN']


class QuantizedConv2dWithBN(Layer):
    """The :class:`QuantizedConv2dWithBN` class is a quantized convolutional layer with BN, which weights are 'bitW' bits and
    the output of the previous layer are 'bitA' bits while inferencing.

    Note that, the bias vector would keep the same.

    Parameters
    ----------
    prev_layer : :class:`Layer`
        Previous layer.
    n_filter : int
        The number of filters.
    filter_size : tuple of int
        The filter size (height, width).
    strides : tuple of int
        The sliding window strides of corresponding input dimensions.
        It must be in the same order as the ``shape`` parameter.
    padding : str
        The padding algorithm type: "SAME" or "VALID".
    act : activation function
        The activation function of this layer.
    decay : float
        A decay factor for `ExponentialMovingAverage`.
        Suggest to use a large value for large dataset.
    epsilon : float
        Eplison.
    is_train : boolean
        Is being used for training or inference.
    beta_init : initializer or None
        The initializer for initializing beta, if None, skip beta.
        Usually you should not skip beta unless you know what happened.
    gamma_init : initializer or None
        The initializer for initializing gamma, if None, skip gamma.
    bitW : int
        The bits of this layer's parameter
    bitA : int
        The bits of the output of previous layer
    decay : float
        A decay factor for `ExponentialMovingAverage`.
        Suggest to use a large value for large dataset.
    epsilon : float
        Eplison.
    is_train : boolean
        Is being used for training or inference.
    beta_init : initializer or None
        The initializer for initializing beta, if None, skip beta.
        Usually you should not skip beta unless you know what happened.
    gamma_init : initializer or None
        The initializer for initializing gamma, if None, skip gamma.
    gemmlowp_at_inference : boolean
        If True, use gemmlowp instead of ``tf.matmul`` (gemm) for inference. (TODO).
    W_init : initializer
        The initializer for the the weight matrix.
    W_init_args : dictionary
        The arguments for the weight matrix initializer.
    use_cudnn_on_gpu : bool
        Default is False.
    data_format : str
        "NHWC" or "NCHW", default is "NHWC".
    name : str
        A unique layer name.

    Examples
    ---------
    >>> import tensorflow as tf
    >>> import tensorlayer as tl
    >>> x = tf.placeholder(tf.float32, [None, 256, 256, 3])
    >>> net = tl.layers.InputLayer(x, name='input')
    >>> net = tl.layers.QuantizedConv2dWithBN(net, 64, (5, 5), (1, 1),  act=tf.nn.relu, padding='SAME', is_train=is_train, bitW=bitW, bitA=bitA, name='qcnnbn1')
    >>> net = tl.layers.MaxPool2d(net, (3, 3), (2, 2), padding='SAME', name='pool1')
    ...
    >>> net = tl.layers.QuantizedConv2dWithBN(net, 64, (5, 5), (1, 1), padding='SAME', act=tf.nn.relu, is_train=is_train,  bitW=bitW, bitA=bitA, name='qcnnbn2')
    >>> net = tl.layers.MaxPool2d(net, (3, 3), (2, 2), padding='SAME', name='pool2')
    ...
    """

    @deprecated_alias(layer='prev_layer', end_support_version=1.9)  # TODO remove this line for the 1.9 release
    def __init__(
            self,
            prev_layer,
            n_filter=32,
            filter_size=(3, 3),
            strides=(1, 1),
            padding='SAME',
            act=None,
            decay=0.9,
            epsilon=1e-5,
            is_train=False,
            gamma_init=tf.ones_initializer,
            beta_init=tf.zeros_initializer,
            bitW=8,
            bitA=8,
            gemmlowp_at_inference=False,
            W_init=tf.truncated_normal_initializer(stddev=0.02),
            W_init_args=None,
            use_cudnn_on_gpu=True,
            data_format=None,
            name='quantized_conv2d',
    ):
        super(QuantizedConv2dWithBN, self).__init__(prev_layer=prev_layer, act=act, W_init_args=W_init_args, name=name)

        logging.info(
            "QuantizedConv2dWithBN %s: n_filter: %d filter_size: %s strides: %s pad: %s act: %s " % (
                self.name, n_filter, filter_size, str(strides), padding, self.act.__name__
                if self.act is not None else 'No Activation'
            )
        )

        x = self.inputs
        self.inputs = quantize_active_overflow(self.inputs, bitA)  # Do not remove

        if gemmlowp_at_inference:
            raise Exception("TODO. The current version use tf.matmul for inferencing.")

        if len(strides) != 2:
            raise ValueError("len(strides) should be 2.")

        try:
            input_channels = int(prev_layer.outputs.get_shape()[-1])
        except TypeError:  # if input_channels is ?, it happens when using Spatial Transformer Net
            input_channels = 1
            logging.warning("[warnings] unknow input channels, set to 1")

        shape = (filter_size[0], filter_size[1], input_channels, n_filter)
        strides = (1, strides[0], strides[1], 1)

        with tf.variable_scope(name):
            W = self._get_tf_variable(
                name='W_conv2d', shape=shape, initializer=W_init, dtype=self.inputs.dtype, **self.W_init_args
            )

            conv = tf.nn.conv2d(
                x, W, strides=strides, padding=padding, use_cudnn_on_gpu=use_cudnn_on_gpu, data_format=data_format
            )

            para_bn_shape = conv.get_shape()[-1:]

            if gamma_init:
                scale_para = self._get_tf_variable(
                    name='scale_para', shape=para_bn_shape, initializer=gamma_init, dtype=self.inputs.dtype,
                    trainable=is_train
                )
            else:
                scale_para = None

            if beta_init:
                offset_para = self._get_tf_variable(
                    name='offset_para', shape=para_bn_shape, initializer=beta_init, dtype=self.inputs.dtype,
                    trainable=is_train
                )
            else:
                offset_para = None

            moving_mean = self._get_tf_variable(
                'moving_mean', para_bn_shape, initializer=tf.constant_initializer(1.), dtype=self.inputs.dtype,
                trainable=False
            )

            moving_variance = self._get_tf_variable(
                'moving_variance',
                para_bn_shape,
                initializer=tf.constant_initializer(1.),
                dtype=self.inputs.dtype,
                trainable=False,
            )

            mean, variance = tf.nn.moments(conv, list(range(len(conv.get_shape()) - 1)))

            update_moving_mean = moving_averages.assign_moving_average(
                moving_mean, mean, decay, zero_debias=False
            )  # if zero_debias=True, has bias

            update_moving_variance = moving_averages.assign_moving_average(
                moving_variance, variance, decay, zero_debias=False
            )  # if zero_debias=True, has bias

            def mean_var_with_update():
                with tf.control_dependencies([update_moving_mean, update_moving_variance]):
                    return tf.identity(mean), tf.identity(variance)

            if is_train:
                mean, var = mean_var_with_update()
            else:
                mean, var = moving_mean, moving_variance

            w_fold = w_fold(W, scale_para, var, epsilon)
            bias_fold = bias_fold(offset_para, scale_para, mean, var, epsilon)

            W = quantize_weight_overflow(w_fold, bitW)

            conv_fold = tf.nn.conv2d(
                self.inputs, W, strides=strides, padding=padding, use_cudnn_on_gpu=use_cudnn_on_gpu,
                data_format=data_format
            )

            self.outputs = tf.nn.bias_add(conv_fold, bias_fold, name='bn_bias_add')

            self.outputs = self._apply_activation(self.outputs)

        self._add_layers(self.outputs)
        self._add_params(self._local_weights)


def w_fold(w, gama, var, epsilon):
    return tf.div(tf.multiply(gama, w), tf.sqrt(var + epsilon))


def bias_fold(beta, gama, mean, var, epsilon):
    return tf.subtract(beta, tf.div(tf.multiply(gama, mean), tf.sqrt(var + epsilon)))
