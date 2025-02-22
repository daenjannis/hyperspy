# -*- coding: utf-8 -*-
# Copyright 2007-2021 The HyperSpy developers
#
# This file is part of  HyperSpy.
#
#  HyperSpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
#  HyperSpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with  HyperSpy.  If not, see <http://www.gnu.org/licenses/>.

import logging
from functools import partial
import warnings

import numpy as np
import dask.array as da
import dask.delayed as dd
import dask
from dask.diagnostics import ProgressBar
from itertools import product
from packaging.version import Version

from hyperspy.signal import BaseSignal
from hyperspy.defaults_parser import preferences
from hyperspy.docstrings.signal import SHOW_PROGRESSBAR_ARG
from hyperspy.exceptions import VisibleDeprecationWarning
from hyperspy.external.progressbar import progressbar
from hyperspy.misc.array_tools import (_requires_linear_rebin,
                                       get_signal_chunk_slice)
from hyperspy.misc.hist_tools import histogram_dask
from hyperspy.misc.machine_learning import import_sklearn
from hyperspy.misc.utils import (multiply, dummy_context_manager, isiterable,
                                 process_function_blockwise, guess_output_signal_size,)


_logger = logging.getLogger(__name__)

lazyerror = NotImplementedError('This method is not available in lazy signals')


def to_array(thing, chunks=None):
    """Accepts BaseSignal, dask or numpy arrays and always produces either
    numpy or dask array.

    Parameters
    ----------
    thing : {BaseSignal, dask.array.Array, numpy.ndarray}
        the thing to be converted
    chunks : {None, tuple of tuples}
        If None, the returned value is a numpy array. Otherwise returns dask
        array with the chunks as specified.

    Returns
    -------
    res : {numpy.ndarray, dask.array.Array}
    """
    if thing is None:
        return None
    if isinstance(thing, BaseSignal):
        thing = thing.data
    if chunks is None:
        if isinstance(thing, da.Array):
            thing = thing.compute()
        if isinstance(thing, np.ndarray):
            return thing
        else:
            raise ValueError
    else:
        if isinstance(thing, np.ndarray):
            thing = da.from_array(thing, chunks=chunks)
        if isinstance(thing, da.Array):
            if thing.chunks != chunks:
                thing = thing.rechunk(chunks)
            return thing
        else:
            raise ValueError


class LazySignal(BaseSignal):
    """A Lazy Signal instance that delays computation until explicitly saved
    (assuming storing the full result of computation in memory is not feasible)
    """
    _lazy = True

    def compute(self, close_file=False, show_progressbar=None, **kwargs):
        """Attempt to store the full signal in memory.

        Parameters
        ----------
        close_file : bool, default False
            If True, attemp to close the file associated with the dask
            array data if any. Note that closing the file will make all other
            associated lazy signals inoperative.
        %s

        Returns
        -------
        None

        """
        if "progressbar" in kwargs:
            warnings.warn(
                "The `progressbar` keyword is deprecated and will be removed "
                "in HyperSpy 2.0. Use `show_progressbar` instead.",
                VisibleDeprecationWarning,
            )
            show_progressbar = kwargs["progressbar"]

        if show_progressbar is None:
            show_progressbar = preferences.General.show_progressbar

        cm = ProgressBar if show_progressbar else dummy_context_manager

        with cm():
            da = self.data
            data = da.compute()
            if close_file:
                self.close_file()
            self.data = data

        self._lazy = False
        self._assign_subclass()

    compute.__doc__ %= SHOW_PROGRESSBAR_ARG

    def rechunk(self,
                nav_chunks="auto",
                sig_chunks=-1,
                inplace=True,
                **kwargs):
        """Rechunks the data using the same rechunking formula from Dask
        expect that the navigation and signal chunks are defined seperately.
        Note, for most functions sig_chunks should remain ``None`` so that it
        spans the entire signal axes.

        Parameters
        ----------
        nav_chunks : {tuple, int, "auto", None}
            The navigation block dimensions to create.
            -1 indicates the full size of the corresponding dimension.
            Default is “auto” which automatically determines chunk sizes.
        sig_chunks : {tuple, int, "auto", None}
            The signal block dimensions to create.
            -1 indicates the full size of the corresponding dimension.
            Default is -1 which automatically spans the full signal dimension
        **kwargs : dict
            Any other keyword arguments for :py:func:`dask.array.rechunk`.
        """
        if not isinstance(sig_chunks, tuple):
            sig_chunks = (sig_chunks,)*len(self.axes_manager.signal_shape)
        if not isinstance(nav_chunks, tuple):
            nav_chunks = (nav_chunks,)*len(self.axes_manager.navigation_shape)
        new_chunks = nav_chunks + sig_chunks
        if inplace:
            self.data = self.data.rechunk(new_chunks,
                                          **kwargs)
        else:
            return self._deepcopy_with_new_data(self.data.rechunk(new_chunks,
                                                                  **kwargs)
                                                )

    def close_file(self):
        """Closes the associated data file if any.

        Currently it only supports closing the file associated with a dask
        array created from an h5py DataSet (default HyperSpy hdf5 reader).

        """
        try:
            self._get_file_handle().close()
        except AttributeError:
            _logger.warning("Failed to close lazy signal file")

    def _get_file_handle(self, warn=True):
        """Return file handle when possible; currently only hdf5 file are
        supported.
        """
        arrkey = None
        for key in self.data.dask.keys():
            if "array-original" in key:
                arrkey = key
                break
        if arrkey:
            try:
                return self.data.dask[arrkey].file
            except (AttributeError, ValueError):
                if warn:
                    _logger.warning("Failed to retrieve file handle, either "
                                    "the file is already closed or it is not "
                                    "an hdf5 file.")

    def _get_dask_chunks(self, axis=None, dtype=None):
        """Returns dask chunks.

        Aims:
            - Have at least one signal (or specified axis) in a single chunk,
              or as many as fit in memory

        Parameters
        ----------
        axis : {int, string, None, axis, tuple}
            If axis is None (default), returns chunks for current data shape so
            that at least one signal is in the chunk. If an axis is specified,
            only that particular axis is guaranteed to be "not sliced".
        dtype : {string, np.dtype}
            The dtype of target chunks.

        Returns
        -------
        Tuple of tuples, dask chunks
        """
        dc = self.data
        dcshape = dc.shape
        for _axis in self.axes_manager._axes:
            if _axis.index_in_array < len(dcshape):
                _axis.size = int(dcshape[_axis.index_in_array])

        if axis is not None:
            need_axes = self.axes_manager[axis]
            if not np.iterable(need_axes):
                need_axes = [need_axes, ]
        else:
            need_axes = self.axes_manager.signal_axes

        if dtype is None:
            dtype = dc.dtype
        elif not isinstance(dtype, np.dtype):
            dtype = np.dtype(dtype)
        typesize = max(dtype.itemsize, dc.dtype.itemsize)
        want_to_keep = multiply([ax.size for ax in need_axes]) * typesize

        # @mrocklin reccomends to have around 100MB chunks, so we do that:
        num_that_fit = int(100. * 2.**20 / want_to_keep)

        # want to have at least one "signal" per chunk
        if num_that_fit < 2:
            chunks = [tuple(1 for _ in range(i)) for i in dc.shape]
            for ax in need_axes:
                chunks[ax.index_in_array] = dc.shape[ax.index_in_array],
            return tuple(chunks)

        sizes = [
            ax.size for ax in self.axes_manager._axes if ax not in need_axes
        ]
        indices = [
            ax.index_in_array for ax in self.axes_manager._axes
            if ax not in need_axes
        ]

        while True:
            if multiply(sizes) <= num_that_fit:
                break

            i = np.argmax(sizes)
            sizes[i] = np.floor(sizes[i] / 2)
        chunks = []
        ndim = len(dc.shape)
        for i in range(ndim):
            if i in indices:
                size = float(dc.shape[i])
                split_array = np.array_split(
                    np.arange(size), np.ceil(size / sizes[indices.index(i)]))
                chunks.append(tuple(len(sp) for sp in split_array))
            else:
                chunks.append((dc.shape[i], ))
        return tuple(chunks)

    def _get_navigation_chunk_size(self):
        nav_axes = self.axes_manager.navigation_indices_in_array
        nav_chunks = tuple([self.data.chunks[i] for i in sorted(nav_axes)])
        return nav_chunks

    def _make_lazy(self, axis=None, rechunk=False, dtype=None):
        self.data = self._lazy_data(axis=axis, rechunk=rechunk, dtype=dtype)

    def change_dtype(self, dtype, rechunk=True):
        # To be consistent with the rechunk argument of other method, we use
        # 'dask_auto' in favour of a chunking which doesn't split signal space.
        if rechunk:
            rechunk = 'dask_auto'
        from hyperspy.misc import rgb_tools
        if not isinstance(dtype, np.dtype) and (dtype not in
                                                rgb_tools.rgb_dtypes):
            dtype = np.dtype(dtype)
        super().change_dtype(dtype)
        self._make_lazy(rechunk=rechunk, dtype=dtype)
    change_dtype.__doc__ = BaseSignal.change_dtype.__doc__

    def _lazy_data(self, axis=None, rechunk=True, dtype=None):
        """Return the data as a dask array, rechunked if necessary.

        Parameters
        ----------
        axis: None, DataAxis or tuple of data axes
            The data axis that must not be broken into chunks when `rechunk`
            is `True`. If None, it defaults to the current signal axes.
        rechunk: bool, "dask_auto"
            If `True`, it rechunks the data if necessary making sure that the
            axes in ``axis`` are not split into chunks. If `False` it does
            not rechunk at least the data is not a dask array, in which case
            it chunks as if rechunk was `True`. If "dask_auto", rechunk if
            necessary using dask's automatic chunk guessing.

        """
        if rechunk == "dask_auto":
            new_chunks = "auto"
        else:
            new_chunks = self._get_dask_chunks(axis=axis, dtype=dtype)
        if isinstance(self.data, da.Array):
            res = self.data
            if self.data.chunks != new_chunks and rechunk:
                _logger.info(
                    "Rechunking.\nOriginal chunks: %s" % str(self.data.chunks))
                res = self.data.rechunk(new_chunks)
                _logger.info(
                    "Final chunks: %s " % str(res.chunks))
        else:
            if isinstance(self.data, np.ma.masked_array):
                data = np.where(self.data.mask, np.nan, self.data)
            else:
                data = self.data
            res = da.from_array(data, chunks=new_chunks)
        assert isinstance(res, da.Array)
        return res

    def _apply_function_on_data_and_remove_axis(self, function, axes,
                                                out=None, rechunk=True):
        def get_dask_function(numpy_name):
            # Translate from the default numpy to dask functions
            translations = {'amax': 'max', 'amin': 'min'}
            if numpy_name in translations:
                numpy_name = translations[numpy_name]
            return getattr(da, numpy_name)

        function = get_dask_function(function.__name__)
        axes = self.axes_manager[axes]
        if not np.iterable(axes):
            axes = (axes, )
        ar_axes = tuple(ax.index_in_array for ax in axes)
        if len(ar_axes) == 1:
            ar_axes = ar_axes[0]
        # For reduce operations the actual signal and navigation
        # axes configuration does not matter. Hence we leave
        # dask guess the chunks
        if rechunk is True:
            rechunk = "dask_auto"
        current_data = self._lazy_data(rechunk=rechunk)
        # Apply reducing function
        new_data = function(current_data, axis=ar_axes)
        if not new_data.ndim:
            new_data = new_data.reshape((1, ))
        if out:
            if out.data.shape == new_data.shape:
                out.data = new_data
                out.events.data_changed.trigger(obj=out)
            else:
                raise ValueError(
                    "The output shape %s does not match  the shape of "
                    "`out` %s" % (new_data.shape, out.data.shape))
        else:
            s = self._deepcopy_with_new_data(new_data)
            s._remove_axis([ax.index_in_axes_manager for ax in axes])
            return s

    def rebin(self, new_shape=None, scale=None,
              crop=False, dtype=None, out=None, rechunk=True):
        factors = self._validate_rebin_args_and_get_factors(
            new_shape=new_shape,
            scale=scale)
        if _requires_linear_rebin(arr=self.data, scale=factors):
            if new_shape:
                raise NotImplementedError(
                    "Lazy rebin requires that the new shape is a divisor "
                    "of the original signal shape e.g. if original shape "
                    "(10| 6), new_shape=(5| 3) is valid, (3 | 4) is not.")
            else:
                raise NotImplementedError(
                    "Lazy rebin requires scale to be integer and divisor of the "
                    "original signal shape")
        axis = {ax.index_in_array: ax
                for ax in self.axes_manager._axes}[factors.argmax()]
        self._make_lazy(axis=axis, rechunk=rechunk)
        return super().rebin(new_shape=new_shape, scale=scale, crop=crop,
                             dtype=dtype, out=out)
    rebin.__doc__ = BaseSignal.rebin.__doc__

    def __array__(self, dtype=None):
        return self.data.__array__(dtype=dtype)

    def _make_sure_data_is_contiguous(self):
        self._make_lazy(rechunk=True)

    def diff(self, axis, order=1, out=None, rechunk=True):
        if not self.axes_manager[axis].is_uniform:
            raise NotImplementedError(
            "Performing a numerical difference on a non-uniform axis "
            "is not implemented. Consider using `derivative` instead."
        )
        arr_axis = self.axes_manager[axis].index_in_array

        def dask_diff(arr, n, axis):
            # assume arr is da.Array already
            n = int(n)
            if n == 0:
                return arr
            if n < 0:
                raise ValueError("order must be positive")
            nd = len(arr.shape)
            slice1 = [slice(None)] * nd
            slice2 = [slice(None)] * nd
            slice1[axis] = slice(1, None)
            slice2[axis] = slice(None, -1)
            slice1 = tuple(slice1)
            slice2 = tuple(slice2)
            if n > 1:
                return dask_diff(arr[slice1] - arr[slice2], n - 1, axis=axis)
            else:
                return arr[slice1] - arr[slice2]

        current_data = self._lazy_data(axis=axis, rechunk=rechunk)
        new_data = dask_diff(current_data, order, arr_axis)
        if not new_data.ndim:
            new_data = new_data.reshape((1, ))

        s = out or self._deepcopy_with_new_data(new_data)
        if out:
            if out.data.shape == new_data.shape:
                out.data = new_data
            else:
                raise ValueError(
                    "The output shape %s does not match  the shape of "
                    "`out` %s" % (new_data.shape, out.data.shape))
        axis2 = s.axes_manager[axis]
        new_offset = self.axes_manager[axis].offset + (order * axis2.scale / 2)
        axis2.offset = new_offset
        s.get_dimensions_from_data()
        if out is None:
            return s
        else:
            out.events.data_changed.trigger(obj=out)

    diff.__doc__ = BaseSignal.diff.__doc__

    def integrate_simpson(self, axis, out=None):
        axis = self.axes_manager[axis]
        from scipy import integrate
        axis = self.axes_manager[axis]
        data = self._lazy_data(axis=axis, rechunk=True)
        new_data = data.map_blocks(
            integrate.simps,
            x=axis.axis,
            axis=axis.index_in_array,
            drop_axis=axis.index_in_array,
            dtype=data.dtype)
        s = out or self._deepcopy_with_new_data(new_data)
        if out:
            if out.data.shape == new_data.shape:
                out.data = new_data
                out.events.data_changed.trigger(obj=out)
            else:
                raise ValueError(
                    "The output shape %s does not match  the shape of "
                    "`out` %s" % (new_data.shape, out.data.shape))
        else:
            s._remove_axis(axis.index_in_axes_manager)
            return s

    integrate_simpson.__doc__ = BaseSignal.integrate_simpson.__doc__

    def valuemax(self, axis, out=None, rechunk=True):
        idx = self.indexmax(axis, rechunk=rechunk)
        old_data = idx.data
        data = old_data.map_blocks(
            lambda x: self.axes_manager[axis].index2value(x))
        if out is None:
            idx.data = data
            return idx
        else:
            out.data = data
            out.events.data_changed.trigger(obj=out)

    valuemax.__doc__ = BaseSignal.valuemax.__doc__

    def valuemin(self, axis, out=None, rechunk=True):
        idx = self.indexmin(axis, rechunk=rechunk)
        old_data = idx.data
        data = old_data.map_blocks(
            lambda x: self.axes_manager[axis].index2value(x))
        if out is None:
            idx.data = data
            return idx
        else:
            out.data = data
            out.events.data_changed.trigger(obj=out)

    valuemin.__doc__ = BaseSignal.valuemin.__doc__

    def get_histogram(self, bins='fd', out=None, rechunk=True, **kwargs):
        if 'range_bins' in kwargs:
            _logger.warning("'range_bins' argument not supported for lazy "
                            "signals")
            del kwargs['range_bins']
        from hyperspy.signals import Signal1D
        data = self._lazy_data(rechunk=rechunk).flatten()
        hist, bin_edges = histogram_dask(data, bins=bins, **kwargs)
        if out is None:
            hist_spec = Signal1D(hist)
            hist_spec._lazy = True
            hist_spec._assign_subclass()
        else:
            hist_spec = out
            # we always overwrite the data because the computation is lazy ->
            # the result signal is lazy. Assume that the `out` is already lazy
            hist_spec.data = hist
        hist_spec.axes_manager[0].scale = bin_edges[1] - bin_edges[0]
        hist_spec.axes_manager[0].offset = bin_edges[0]
        hist_spec.axes_manager[0].size = hist.shape[-1]
        hist_spec.axes_manager[0].name = 'value'
        hist_spec.axes_manager[0].is_binned = True
        hist_spec.metadata.General.title = (
            self.metadata.General.title + " histogram")
        if out is None:
            return hist_spec
        else:
            out.events.data_changed.trigger(obj=out)

    get_histogram.__doc__ = BaseSignal.get_histogram.__doc__

    @staticmethod
    def _estimate_poissonian_noise_variance(dc, gain_factor, gain_offset,
                                            correlation_factor):
        variance = (dc * gain_factor + gain_offset) * correlation_factor
        # The lower bound of the variance is the gaussian noise.
        variance = da.clip(variance, gain_offset * correlation_factor, np.inf)
        return variance

    # def _get_navigation_signal(self, data=None, dtype=None):
    # return super()._get_navigation_signal(data=data, dtype=dtype).as_lazy()

    # _get_navigation_signal.__doc__ = BaseSignal._get_navigation_signal.__doc__

    # def _get_signal_signal(self, data=None, dtype=None):
    #     return super()._get_signal_signal(data=data, dtype=dtype).as_lazy()

    # _get_signal_signal.__doc__ = BaseSignal._get_signal_signal.__doc__

    def _calculate_summary_statistics(self, rechunk=True):
        if rechunk is True:
            # Use dask auto rechunk instead of HyperSpy's one, what should be
            # better for these operations
            rechunk = "dask_auto"
        data = self._lazy_data(rechunk=rechunk)
        _raveled = data.ravel()
        _mean, _std, _min, _q1, _q2, _q3, _max = da.compute(
            da.nanmean(data),
            da.nanstd(data),
            da.nanmin(data),
            da.percentile(_raveled, [25, ]),
            da.percentile(_raveled, [50, ]),
            da.percentile(_raveled, [75, ]),
            da.nanmax(data), )
        return _mean, _std, _min, _q1, _q2, _q3, _max

    def _map_all(self, function, inplace=True, **kwargs):
        calc_result = dd(function)(self.data, **kwargs)
        if inplace:
            self.data = da.from_delayed(calc_result, shape=self.data.shape,
                                        dtype=self.data.dtype)
            return None
        return self._deepcopy_with_new_data(calc_result)

    def _map_iterate(self,
                     function,
                     iterating_kwargs=None,
                     show_progressbar=None,
                     parallel=None,
                     max_workers=None,
                     ragged=False,
                     inplace=True,
                     output_signal_size=None,
                     output_dtype=None,
                     **kwargs):
        # unpacking keyword arguments
        if iterating_kwargs is None:
            iterating_kwargs = {}
        elif isinstance(iterating_kwargs, (tuple, list)):
            iterating_kwargs = dict((k, v) for k, v in iterating_kwargs)

        nav_indexes = self.axes_manager.navigation_indices_in_array
        if ragged and inplace:
            raise ValueError("Ragged and inplace are not compatible with a lazy signal")
        chunk_span = np.equal(self.data.chunksize, self.data.shape)
        chunk_span = [chunk_span[i] for i in self.axes_manager.signal_indices_in_array]
        if not all(chunk_span):
            _logger.info("The chunk size needs to span the full signal size, rechunking...")
            old_sig = self.rechunk(inplace=False)
        else:
            old_sig = self
        autodetermine = (output_signal_size is None or output_dtype is None) # try to guess output dtype and sig size?
        nav_chunks = old_sig._get_navigation_chunk_size()
        args = ()
        arg_keys = ()

        for key in iterating_kwargs:
            if not isinstance(iterating_kwargs[key], BaseSignal):
                iterating_kwargs[key] = BaseSignal(iterating_kwargs[key].T).T
                warnings.warn(
                    "Passing arrays as keyword arguments can be ambigous. "
                    "This is deprecated and will be removed in HyperSpy 2.0. "
                    "Pass signal instances instead.",
                    VisibleDeprecationWarning)
            if iterating_kwargs[key]._lazy:
                if iterating_kwargs[key]._get_navigation_chunk_size() != nav_chunks:
                    iterating_kwargs[key].rechunk(nav_chunks=nav_chunks)
            else:
                iterating_kwargs[key] = iterating_kwargs[key].as_lazy()
                iterating_kwargs[key].rechunk(nav_chunks=nav_chunks)
            extra_dims = (len(old_sig.axes_manager.signal_shape) -
                          len(iterating_kwargs[key].axes_manager.signal_shape))
            if extra_dims > 0:
                old_shape = iterating_kwargs[key].data.shape
                new_shape = old_shape + (1,)*extra_dims
                args += (iterating_kwargs[key].data.reshape(new_shape), )
            else:
                args += (iterating_kwargs[key].data, )
            arg_keys += (key,)

        if autodetermine: #trying to guess the output d-type and size from one signal
            testing_kwargs = {}
            for key in iterating_kwargs:
                test_ind = (0,) * len(old_sig.axes_manager.navigation_axes)
                testing_kwargs[key] = np.squeeze(iterating_kwargs[key].inav[test_ind].data).compute()
            testing_kwargs = {**kwargs, **testing_kwargs}
            test_data = np.array(old_sig.inav[(0,) * len(old_sig.axes_manager.navigation_shape)].data.compute())
            output_signal_size, output_dtype = guess_output_signal_size(test_signal=test_data,
                                                                        function=function,
                                                                        ragged=ragged,
                                                                        **testing_kwargs)
        # Dropping/Adding Axes
        if output_signal_size == old_sig.axes_manager.signal_shape:
            drop_axis = None
            new_axis = None
            axes_changed = False
        else:
            axes_changed = True
            if len(output_signal_size) != len(old_sig.axes_manager.signal_shape):
                drop_axis = old_sig.axes_manager.signal_indices_in_array
                new_axis = tuple(range(len(nav_indexes), len(nav_indexes) + len(output_signal_size)))
            else:
                drop_axis = [it for (o, i, it) in zip(output_signal_size,
                                                      old_sig.axes_manager.signal_shape,
                                                      old_sig.axes_manager.signal_indices_in_array)
                             if o != i]
                new_axis = drop_axis
        chunks = tuple([old_sig.data.chunks[i] for i in sorted(nav_indexes)]) + output_signal_size
        mapped = da.map_blocks(process_function_blockwise,
                               old_sig.data,
                               *args,
                               function=function,
                               nav_indexes=nav_indexes,
                               drop_axis=drop_axis,
                               new_axis=new_axis,
                               output_signal_size=output_signal_size,
                               dtype=output_dtype,
                               chunks=chunks,
                               arg_keys=arg_keys,
                               **kwargs)
        if inplace:
            self.data = mapped
            sig = self
        else:
            sig = self._deepcopy_with_new_data(mapped)
            if ragged:
                axes_dicts = self.axes_manager._get_axes_dicts(
                    self.axes_manager.navigation_axes
                    )
                sig.axes_manager.__init__(axes_dicts)
                sig.axes_manager._ragged = True
                sig._assign_subclass()
                return sig

        # remove if too many axes
        if axes_changed:
            sig.axes_manager.remove(sig.axes_manager.signal_axes[len(output_signal_size):])
            # add additional required axes
            for ind in range(
                    len(output_signal_size) - sig.axes_manager.signal_dimension, 0, -1):
                sig.axes_manager._append_axis(size=output_signal_size[-ind],
                                              navigate=False)
        if not ragged:
            sig.get_dimensions_from_data()
        return sig

    def _block_iterator(self,
                        flat_signal=True,
                        get=dask.threaded.get,
                        navigation_mask=None,
                        signal_mask=None):
        """A function that allows iterating lazy signal data by blocks,
        defining the dask.Array.

        Parameters
        ----------
        flat_signal: bool
            returns each block flattened, such that the shape (for the
            particular block) is (navigation_size, signal_size), with
            optionally masked elements missing. If false, returns
            the equivalent of s.inav[{blocks}].data, where masked elements are
            set to np.nan or 0.
        get : dask scheduler
            the dask scheduler to use for computations;
            default `dask.threaded.get`
        navigation_mask : {BaseSignal, numpy array, dask array}
            The navigation locations marked as True are not returned (flat) or
            set to NaN or 0.
        signal_mask : {BaseSignal, numpy array, dask array}
            The signal locations marked as True are not returned (flat) or set
            to NaN or 0.

        """
        self._make_lazy()
        data = self._data_aligned_with_axes
        nav_chunks = data.chunks[:self.axes_manager.navigation_dimension]
        indices = product(*[range(len(c)) for c in nav_chunks])
        signalsize = self.axes_manager.signal_size
        sig_reshape = (signalsize,) if signalsize else ()
        data = data.reshape((self.axes_manager.navigation_shape[::-1] +
                             sig_reshape))

        if signal_mask is None:
            signal_mask = slice(None) if flat_signal else \
                np.zeros(self.axes_manager.signal_size, dtype='bool')
        else:
            try:
                signal_mask = to_array(signal_mask).ravel()
            except ValueError:
                # re-raise with a message
                raise ValueError("signal_mask has to be a signal, numpy or"
                                 " dask array, but "
                                 "{} was given".format(type(signal_mask)))
            if flat_signal:
                signal_mask = ~signal_mask

        if navigation_mask is None:
            nav_mask = da.zeros(
                self.axes_manager.navigation_shape[::-1],
                chunks=nav_chunks,
                dtype='bool')
        else:
            try:
                nav_mask = to_array(navigation_mask, chunks=nav_chunks)
            except ValueError:
                # re-raise with a message
                raise ValueError("navigation_mask has to be a signal, numpy or"
                                 " dask array, but "
                                 "{} was given".format(type(navigation_mask)))
        if flat_signal:
            nav_mask = ~nav_mask
        for ind in indices:
            chunk = get(data.dask,
                        (data.name, ) + ind + (0,) * bool(signalsize))
            n_mask = get(nav_mask.dask, (nav_mask.name, ) + ind)
            if flat_signal:
                yield chunk[n_mask, ...][..., signal_mask]
            else:
                chunk = chunk.copy()
                value = np.nan if np.can_cast('float', chunk.dtype) else 0
                chunk[n_mask, ...] = value
                chunk[..., signal_mask] = value
                yield chunk.reshape(chunk.shape[:-1] +
                                    self.axes_manager.signal_shape[::-1])

    def decomposition(
        self,
        normalize_poissonian_noise=False,
        algorithm="SVD",
        output_dimension=None,
        signal_mask=None,
        navigation_mask=None,
        get=dask.threaded.get,
        num_chunks=None,
        reproject=True,
        print_info=True,
        **kwargs
    ):
        """Perform Incremental (Batch) decomposition on the data.

        The results are stored in ``self.learning_results``.

        Read more in the :ref:`User Guide <big_data.decomposition>`.

        Parameters
        ----------
        normalize_poissonian_noise : bool, default False
            If True, scale the signal to normalize Poissonian noise using
            the approach described in [KeenanKotula2004]_.
        algorithm : {'SVD', 'PCA', 'ORPCA', 'ORNMF'}, default 'SVD'
            The decomposition algorithm to use.
        output_dimension : int or None, default None
            Number of components to keep/calculate. If None, keep all
            (only valid for 'SVD' algorithm)
        get : dask scheduler
            the dask scheduler to use for computations;
            default `dask.threaded.get`
        num_chunks : int or None, default None
            the number of dask chunks to pass to the decomposition model.
            More chunks require more memory, but should run faster. Will be
            increased to contain at least ``output_dimension`` signals.
        navigation_mask : {BaseSignal, numpy array, dask array}
            The navigation locations marked as True are not used in the
            decomposition. Not implemented for the 'SVD' algorithm.
        signal_mask : {BaseSignal, numpy array, dask array}
            The signal locations marked as True are not used in the
            decomposition. Not implemented for the 'SVD' algorithm.
        reproject : bool, default True
            Reproject data on the learnt components (factors) after learning.
        print_info : bool, default True
            If True, print information about the decomposition being performed.
            In the case of sklearn.decomposition objects, this includes the
            values of all arguments of the chosen sklearn algorithm.
        **kwargs
            passed to the partial_fit/fit functions.

        References
        ----------
        .. [KeenanKotula2004] M. Keenan and P. Kotula, "Accounting for Poisson noise
            in the multivariate analysis of ToF-SIMS spectrum images", Surf.
            Interface Anal 36(3) (2004): 203-212.

        See Also
        --------
        * :py:meth:`~.learn.mva.MVA.decomposition` for non-lazy signals
        * :py:func:`dask.array.linalg.svd`
        * :py:class:`sklearn.decomposition.IncrementalPCA`
        * :py:class:`~.learn.rpca.ORPCA`
        * :py:class:`~.learn.ornmf.ORNMF`

        """
        if kwargs.get("bounds", False):
            warnings.warn(
                "The `bounds` keyword is deprecated and will be removed "
                "in v2.0. Since version > 1.3 this has no effect.",
                VisibleDeprecationWarning,
            )
            kwargs.pop("bounds", None)

        # Deprecate 'ONMF' for 'ORNMF'
        if algorithm == "ONMF":
            warnings.warn(
                "The argument `algorithm='ONMF'` has been deprecated and will "
                "be removed in future. Please use `algorithm='ORNMF'` instead.",
                VisibleDeprecationWarning,
            )
            algorithm = "ORNMF"


        # Check algorithms requiring output_dimension
        algorithms_require_dimension = ["PCA", "ORPCA", "ORNMF"]
        if algorithm in algorithms_require_dimension and output_dimension is None:
            raise ValueError(
                "`output_dimension` must be specified for '{}'".format(algorithm)
            )

        explained_variance = None
        explained_variance_ratio = None

        _al_data = self._data_aligned_with_axes
        nav_chunks = _al_data.chunks[: self.axes_manager.navigation_dimension]
        sig_chunks = _al_data.chunks[self.axes_manager.navigation_dimension :]

        num_chunks = 1 if num_chunks is None else num_chunks
        blocksize = np.min([multiply(ar) for ar in product(*nav_chunks)])
        nblocks = multiply([len(c) for c in nav_chunks])

        if output_dimension and blocksize / output_dimension < num_chunks:
            num_chunks = np.ceil(blocksize / output_dimension)

        blocksize *= num_chunks

        # Initialize print_info
        to_print = [
            "Decomposition info:",
            f"  normalize_poissonian_noise={normalize_poissonian_noise}",
            f"  algorithm={algorithm}",
            f"  output_dimension={output_dimension}"
        ]

        # LEARN
        if algorithm == "PCA":
            if not import_sklearn.sklearn_installed:
                raise ImportError("algorithm='PCA' requires scikit-learn")

            obj = import_sklearn.sklearn.decomposition.IncrementalPCA(n_components=output_dimension)
            method = partial(obj.partial_fit, **kwargs)
            reproject = True
            to_print.extend(["scikit-learn estimator:", obj])

        elif algorithm == "ORPCA":
            from hyperspy.learn.rpca import ORPCA

            batch_size = kwargs.pop("batch_size", None)
            obj = ORPCA(output_dimension, **kwargs)
            method = partial(obj.fit, batch_size=batch_size)

        elif algorithm == "ORNMF":
            from hyperspy.learn.ornmf import ORNMF

            batch_size = kwargs.pop("batch_size", None)
            obj = ORNMF(output_dimension, **kwargs)
            method = partial(obj.fit, batch_size=batch_size)

        elif algorithm != "SVD":
            raise ValueError("'algorithm' not recognised")

        original_data = self.data
        try:
            _logger.info("Performing decomposition analysis")

            if normalize_poissonian_noise:
                _logger.info("Scaling the data to normalize Poissonian noise")

                data = self._data_aligned_with_axes
                ndim = self.axes_manager.navigation_dimension
                sdim = self.axes_manager.signal_dimension
                nm = da.logical_not(
                    da.zeros(self.axes_manager.navigation_shape[::-1], chunks=nav_chunks)
                    if navigation_mask is None
                    else to_array(navigation_mask, chunks=nav_chunks)
                )
                sm = da.logical_not(
                    da.zeros(self.axes_manager.signal_shape[::-1], chunks=sig_chunks)
                    if signal_mask is None
                    else to_array(signal_mask, chunks=sig_chunks)
                )
                ndim = self.axes_manager.navigation_dimension
                sdim = self.axes_manager.signal_dimension
                bH, aG = da.compute(
                    data.sum(axis=tuple(range(ndim))),
                    data.sum(axis=tuple(range(ndim, ndim + sdim))),
                )
                bH = da.where(sm, bH, 1)
                aG = da.where(nm, aG, 1)

                raG = da.sqrt(aG)
                rbH = da.sqrt(bH)

                coeff = raG[(...,) + (None,) * rbH.ndim] * rbH[(None,) * raG.ndim + (...,)]
                coeff.map_blocks(np.nan_to_num)
                coeff = da.where(coeff == 0, 1, coeff)
                data = data / coeff
                self.data = data

            # LEARN
            if algorithm == "SVD":
                reproject = False
                from dask.array.linalg import svd

                try:
                    self._unfolded4decomposition = self.unfold()
                    # TODO: implement masking
                    if navigation_mask is not None or signal_mask is not None:
                        raise NotImplementedError("Masking is not yet implemented for lazy SVD")

                    U, S, V = svd(self.data)

                    if output_dimension is None:
                        min_shape = min(min(U.shape), min(V.shape))
                    else:
                        min_shape = output_dimension

                    U = U[:, :min_shape]
                    S = S[:min_shape]
                    V = V[:min_shape]

                    factors = V.T
                    explained_variance = S ** 2 / self.data.shape[0]
                    loadings = U * S
                finally:
                    if self._unfolded4decomposition is True:
                        self.fold()
                        self._unfolded4decomposition is False
            else:
                self._check_navigation_mask(navigation_mask)
                self._check_signal_mask(signal_mask)
                this_data = []
                try:
                    for chunk in progressbar(
                        self._block_iterator(
                            flat_signal=True,
                            get=get,
                            signal_mask=signal_mask,
                            navigation_mask=navigation_mask,
                        ),
                        total=nblocks,
                        leave=True,
                        desc="Learn",
                    ):
                        this_data.append(chunk)
                        if len(this_data) == num_chunks:
                            thedata = np.concatenate(this_data, axis=0)
                            method(thedata)
                            this_data = []
                    if len(this_data):
                        thedata = np.concatenate(this_data, axis=0)
                        method(thedata)
                except KeyboardInterrupt:  # pragma: no cover
                    pass

            # GET ALREADY CALCULATED RESULTS
            if algorithm == "PCA":
                explained_variance = obj.explained_variance_
                explained_variance_ratio = obj.explained_variance_ratio_
                factors = obj.components_.T

            elif algorithm == "ORPCA":
                factors, loadings = obj.finish()
                loadings = loadings.T

            elif algorithm == "ORNMF":
                factors, loadings = obj.finish()
                loadings = loadings.T

            # REPROJECT
            if reproject:
                if algorithm == "PCA":
                    method = obj.transform

                    def post(a):
                        return np.concatenate(a, axis=0)

                elif algorithm == "ORPCA":
                    method = obj.project

                    def post(a):
                        return np.concatenate(a, axis=1).T

                elif algorithm == "ORNMF":
                    method = obj.project

                    def post(a):
                        return np.concatenate(a, axis=1).T

                _map = map(
                    lambda thing: method(thing),
                    self._block_iterator(
                        flat_signal=True,
                        get=get,
                        signal_mask=signal_mask,
                        navigation_mask=navigation_mask,
                    ),
                )
                H = []
                try:
                    for thing in progressbar(_map, total=nblocks, desc="Project"):
                        H.append(thing)
                except KeyboardInterrupt:  # pragma: no cover
                    pass
                loadings = post(H)

            if explained_variance is not None and explained_variance_ratio is None:
                explained_variance_ratio = explained_variance / explained_variance.sum()

            # RESHUFFLE "blocked" LOADINGS
            ndim = self.axes_manager.navigation_dimension
            if algorithm != "SVD":  # Only needed for online algorithms
                try:
                    loadings = _reshuffle_mixed_blocks(
                        loadings, ndim, (output_dimension,), nav_chunks
                    ).reshape((-1, output_dimension))
                except ValueError:
                    # In case the projection step was not finished, it's left
                    # as scrambled
                    pass
        finally:
            self.data = original_data

        target = self.learning_results
        target.decomposition_algorithm = algorithm
        target.output_dimension = output_dimension
        if algorithm != "SVD":
            target._object = obj
        target.factors = factors
        target.loadings = loadings
        target.explained_variance = explained_variance
        target.explained_variance_ratio = explained_variance_ratio

        # Rescale the results if the noise was normalized
        if normalize_poissonian_noise is True:
            target.factors = target.factors * rbH.ravel()[:, np.newaxis]
            target.loadings = target.loadings * raG.ravel()[:, np.newaxis]

        # Print details about the decomposition we just performed
        if print_info:
            print("\n".join([str(pr) for pr in to_print]))

    def plot(self, navigator='auto', **kwargs):
        if self.axes_manager.ragged:
            raise RuntimeError("Plotting ragged signal is not supported.")
        if isinstance(navigator, str):
            if navigator == 'spectrum':
                # We don't support the 'spectrum' option to keep it simple
                _logger.warning("The `navigator='spectrum'` option is not "
                                "supported for lazy signals, 'auto' is used "
                                "instead.")
                navigator = 'auto'
            if navigator == 'auto':
                nav_dim = self.axes_manager.navigation_dimension
                if nav_dim in [1, 2]:
                    if self.navigator is None:
                        self.compute_navigator()
                    navigator = self.navigator
                elif nav_dim > 2:
                    navigator = 'slider'
        super().plot(navigator=navigator, **kwargs)

    def compute_navigator(self, index=None, chunks_number=None,
                          show_progressbar=None):
        """
        Compute the navigator by taking the sum over a single chunk contained
        the specified coordinate. Taking the sum over a single chunk is a
        computationally efficient approach to compute the navigator. The data
        can be rechunk by specifying the ``chunks_number`` argument.

        Parameters
        ----------
        index : (int, float, None) or iterable, optional
            Specified where to take the sum, follows HyperSpy indexing syntax
            for integer and float. If None, the index is the centre of the
            signal_space
        chunks_number : (int, None) or iterable, optional
            Define the number of chunks in the signal space used for rechunk
            the when calculating of the navigator. Useful to define the range
            over which the sum is calculated.
            If None, the existing chunking will be considered when picking the
            chunk used in the navigator calculation.
        %s

        Returns
        -------
        None.

        Note
        ----
        The number of chunks will affect where the sum is taken. If the sum
        needs to be taken in the centre of the signal space (for example, in
        the case of diffraction pattern), the number of chunk needs to be an
        odd number, so that the middle is centered.

        """

        signal_shape = self.axes_manager.signal_shape

        if index is None:
            index = [round(shape / 2) for shape in signal_shape]
        else:
            if not isiterable(index):
                index = [index] * len(signal_shape)
            index = [axis._get_index(_idx)
                     for _idx, axis in zip(index, self.axes_manager.signal_axes)]
        _logger.info(f"Using index: {index}")

        if chunks_number is None:
            chunks = self.data.chunks
        else:
            if not isiterable(chunks_number):
                chunks_number = [chunks_number] * len(signal_shape)
            # Determine the chunk size
            signal_chunks = da.core.normalize_chunks(
                [int(size / cn) for cn, size in zip(chunks_number, signal_shape)],
                shape=signal_shape
                )
            # Needs to reverse the chunks list to match dask chunking order
            signal_chunks = list(signal_chunks)[::-1]
            navigation_chunks = ['auto'] * len(self.axes_manager.navigation_shape)
            if Version(dask.__version__) >= Version("2.30.0"):
                kwargs = {'balance':True}
            else:
                kwargs = {}
            chunks = self.data.rechunk([*navigation_chunks, *signal_chunks],
                                       **kwargs).chunks

        # Get the slice of the corresponding chunk
        signal_size = len(signal_shape)
        signal_chunks = tuple(chunks[i-signal_size] for i in range(signal_size))
        _logger.info(f"Signal chunks: {signal_chunks}")
        isig_slice = get_signal_chunk_slice(index, chunks)

        _logger.info(f'Computing sum over signal dimension: {isig_slice}')
        axes = [axis.index_in_array for axis in self.axes_manager.signal_axes]
        navigator = self.isig[isig_slice].sum(axes)
        navigator.compute(show_progressbar=show_progressbar)
        navigator.original_metadata.set_item('sum_from', str(isig_slice))

        self.navigator = navigator.T

    compute_navigator.__doc__ %= SHOW_PROGRESSBAR_ARG


def _reshuffle_mixed_blocks(array, ndim, sshape, nav_chunks):
    """Reshuffles dask block-shuffled array

    Parameters
    ----------
    array : np.ndarray
        the array to reshuffle
    ndim : int
        the number of navigation (shuffled) dimensions
    sshape : tuple of ints
        The shape
    """
    splits = np.cumsum([multiply(ar)
                        for ar in product(*nav_chunks)][:-1]).tolist()
    if splits:
        all_chunks = [
            ar.reshape(shape + sshape)
            for shape, ar in zip(
                product(*nav_chunks), np.split(array, splits))
        ]

        def split_stack_list(what, step, axis):
            total = len(what)
            if total != step:
                return [
                    np.concatenate(
                        what[i:i + step], axis=axis)
                    for i in range(0, total, step)
                ]
            else:
                return np.concatenate(what, axis=axis)

        for chunks, axis in zip(nav_chunks[::-1], range(ndim - 1, -1, -1)):
            step = len(chunks)
            all_chunks = split_stack_list(all_chunks, step, axis)
        return all_chunks
    else:
        return array
