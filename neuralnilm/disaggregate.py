from __future__ import division, print_function
import numpy as np
from collections import namedtuple
import csv
from os.path import join, expanduser
import pandas as pd

from neuralnilm.source import standardise


def disag_ae_or_rnn(mains, net, std, max_target_power, stride=1):
    """
    Parameters
    ----------
    mains : 1D np.ndarray
        Watts.
        Mains must be padded with at least `seq_length` elements
        at both ends so the net can slide over the very start and end.
    net : neuralnilm.net.Net
    max_target_power : int
        Watts
    stride : int or None, optional
        if None then stide = seq_length

    Returns
    -------
    estimates : 1D vector
    """
    n_seq_per_batch, seq_length = net.input_shape[:2]
    if stride is None:
        stride = seq_length
    batches = mains_to_batches(mains, n_seq_per_batch, seq_length, std, stride)
    estimates = np.zeros(len(mains), dtype=np.float32)
    assert not seq_length % stride

    # Iterate over each batch
    for batch_i, net_input in enumerate(batches):
        net_output = net.y_pred(net_input)
        batch_start = batch_i * n_seq_per_batch * stride
        for seq_i in range(n_seq_per_batch):
            start_i = batch_start + (seq_i * stride)
            end_i = start_i + seq_length
            n = len(estimates[start_i:end_i])
            # The net output is not necessarily the same length
            # as the mains (because mains might not fit exactly into
            # the number of batches required)
            estimates[start_i:end_i] += net_output[seq_i, :n, 0]

    n_overlaps = seq_length / stride
    estimates /= n_overlaps
    estimates *= max_target_power
    estimates[estimates < 0] = 0
    return estimates


Rectangle = namedtuple('Rectangle', ['left', 'right', 'height'])


def disaggregate_start_stop_end(mains, net, std, stride=1, max_target_power=1):
    """
    Parameters
    ----------
    mains : 1D np.ndarray
        Watts.
        And it is highly advisable to pad `mains` with `seq_length` elements
        at both ends so the net can slide over the very start and end.
    net : neuralnilm.net.Net
    stride : int or None, optional
        if None then stide = seq_length
    max_target_power : int, optional
        Watts

    Returns
    -------
    rectangles : dict
        Each key is an output instance integer.
        Each value is a Rectangle namedtuple with fields:
        - 'start' : int, index into `mains`
        - 'stop' : int, index into `mains`
        - 'height' : float, Watts
    """
    n_seq_per_batch, seq_length = net.input_shape[:2]
    n_outputs = net.output_shape[2]
    if stride is None:
        stride = seq_length
    batches = mains_to_batches(mains, n_seq_per_batch, seq_length, std, stride)
    rectangles = {output_i: [] for output_i in range(n_outputs)}

    # Iterate over each batch
    for batch_i, net_input in enumerate(batches):
        net_output = net.y_pred(net_input)
        batch_start = batch_i * n_seq_per_batch * stride
        for seq_i in range(n_seq_per_batch):
            offset = batch_start + (seq_i * stride)
            for output_i in range(n_outputs):
                net_output_for_seq = net_output[seq_i, :, output_i]
                rect_left = (net_output_for_seq[0] * seq_length) + offset
                rect_left = int(round(rect_left))
                rect_right = (net_output_for_seq[1] * seq_length) + offset
                rect_right = int(round(rect_right))
                rect_height = net_output_for_seq[2] * max_target_power
                rect = Rectangle(
                    left=rect_left, right=rect_right, height=rect_height)
                rectangles[output_i].append(rect)

    return rectangles


def rectangle_filename(output_i, path=''):
    """
    Parameters
    ----------
    output_i : int
    path : string

    Returns
    -------
    full_filename : string
    """
    path = expanduser(path)
    base_filename = 'disag_rectangles_output{:d}.csv'.format(output_i)
    full_filename = join(path, base_filename)
    return full_filename


def save_rectangles(rectangles, path=''):
    """
    Parameters
    ----------
    rectangles : dict
        Output from `disaggregate_start_stop_end()`
    path : string
    """
    for output_i, rects in rectangles.iteritems():
        filename = rectangle_filename(output_i, path)
        print("Saving", filename)
        with open(filename, 'wb') as f:
            writer = csv.writer(f)
            writer.writerow(Rectangle._fields)
            writer.writerows(rects)
        print("Done saving", filename)


def load_rectangles(path=''):
    rectangles = {}
    for output_i in range(256):
        filename = rectangle_filename(output_i, path)
        try:
            f = open(filename, 'rb')
        except IOError:
            if output_i == 0:
                raise IOError(
                    "No rectangle CSV files found in {}".format(path))
            else:
                break
        rects = []
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            left = int(row[0])
            right = int(row[1])
            height = float(row[2])
            rect = Rectangle(left=left, right=right, height=height)
            rects.append(rect)
        f.close()
        rectangles[output_i] = rects

    return rectangles


def rectangles_to_matrix(rectangles, max_appliance_power):
    """
    Parameters
    ----------
    rectangles : list of Rectangles
        Value of dict output from `disaggregate_start_stop_end()`
    max_appliance_power : int or float
        Watts

    Returns
    -------
    matrix : 2D numpy.ndarray
        Normalised to [0, 1]
    """
    n_samples = rectangles[-1].right
    matrix = np.zeros(shape=(max_appliance_power, n_samples), dtype=np.float32)
    for rect in rectangles:
        height = int(round(rect.height))
        matrix[:height, rect.left:rect.right] += 1
    matrix /= matrix.max()
    return matrix


def rectangles_matrix_to_vector(matrix, min_on_power, overlap_threshold=0.5):
    """
    Parameters
    ----------
    matrix : 2D numpy.ndarray
        Output from `rectangles_to_matrix`
    min_on_power : int
        Watts
    overlap_threshold : float, [0, 1]

    Returns
    -------
    vector : 1D numpy.ndarray
        Watts
    """
    n_samples = matrix.shape[1]

    # Zero out any elements less than overlap_threshold
    matrix[matrix < overlap_threshold] = 0

    # Find indicies of non-zero elements
    nonzero_indicies = np.nonzero(matrix)
    del matrix

    # Put indicies into a Pandas Series so we can use
    # Pandas' groupby mechanism
    nonzero_indicies = pd.Series(
        nonzero_indicies[0], index=nonzero_indicies[1])

    # Ignore any values below min_on_power
    nonzero_indicies = nonzero_indicies[nonzero_indicies > min_on_power]

    # nonzero_indicies will have lots of duplicate indicies,
    # one duplicate for every row on that column whose value is above zero.
    # So we group by the indicies and get the max row index
    # to drape a 'hull' over the nonzero elements of matrix.
    grouped = nonzero_indicies.groupby(level=0)
    del nonzero_indicies
    hull = grouped.max()
    del grouped

    vector = np.zeros(n_samples)
    vector[hull.index] = hull.values
    return vector


def mains_to_batches(mains, n_seq_per_batch, seq_length, std, stride=1):
    """
    Parameters
    ----------
    mains : 1D np.ndarray
        Watts.
        And it is highly advisable to pad `mains` with `seq_length` elements
        at both ends so the net can slide over the very start and end.
    std : mains standard deviation
    stride : int, optional

    Returns
    -------
    batches : list of 3D arrays
    """
    assert mains.ndim == 1
    n_mains_samples = len(mains)
    input_shape = (n_seq_per_batch, seq_length, 1)

    # Divide mains data into batches
    n_batches = (n_mains_samples / stride) / n_seq_per_batch
    n_batches = np.ceil(n_batches).astype(int)
    batches = []
    for batch_i in xrange(n_batches):
        batch = np.zeros(input_shape, dtype=np.float32)
        batch_start = batch_i * n_seq_per_batch * stride
        for seq_i in xrange(n_seq_per_batch):
            mains_start_i = batch_start + (seq_i * stride)
            mains_end_i = mains_start_i + seq_length
            seq = mains[mains_start_i:mains_end_i]
            seq_standardised = standardise(seq, how='std=1', std=std)
            batch[seq_i, :len(seq), 0] = seq_standardised
        batches.append(batch)

    return batches

"""
Emacs variables
Local Variables:
compile-command: "cp /home/jack/workspace/python/neuralnilm/neuralnilm_prototype/disaggregate.py /mnt/sshfs/imperial/workspace/python/neuralnilm/neuralnilm_prototype/"
End:
"""
