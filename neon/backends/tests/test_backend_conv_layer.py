# ----------------------------------------------------------------------------
# Copyright 2015 Nervana Systems Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ----------------------------------------------------------------------------
# pylint: skip-file

"""
To test conv layer operations between NervanaGPU, NervanaCPU against numpy.
The numpy implementation is different from what is done underneath NervanaCPU to
be a valid checking. It requires externally pad the input, while NervanaCPU does
not require so
"""
import numpy as np
from operator import mul

from neon.backends.nervanagpu import NervanaGPU
from neon.backends.nervanacpu import NervanaCPU
from timeit import default_timer


def slicable(dim, pad=0):
    """
    colapse outer dimensions into one and preserve inner dimension
    this allows for easy cpu convolution in numpy

    Arguments:
        dim (tuple): dimensions list in a tuple
        pad (int):  how many pixel paddings
    """
    dim0 = reduce(mul, dim[:-1], 1) + pad
    return (dim0, dim[-1])


def pixel_indices(conv, mt, pr, qs):

    T, R, S = conv.TRS
    D, H, W = conv.DHW
    C = conv.C
    HW = H * W
    DHW = D * H * W
    imax = C * DHW

    idx = []
    for c in range(C):
        ci = c * DHW

        for t in range(T):
            z = mt + t
            zi = ci + z * HW
            zb = z >= 0 and z < D

            for r in range(R):
                y = pr + r
                yi = zi + y * W
                yb = zb and y >= 0 and y < H

                for s in range(S):
                    x = qs + s
                    if yb and x >= 0 and x < W:
                        xi = yi + x
                    else:
                        xi = imax  # out of bounds

                    idx.append(xi)
    return idx


def run_backend_conv(lib, layer, I, F, E, dtype):

    beI = lib.array(I, dtype=dtype)
    beF = lib.array(F, dtype=dtype)
    beE = lib.array(E, dtype=dtype)

    beO = lib.zeros(layer.dimO, dtype=dtype)
    lib.fprop_conv(layer, beI, beF, beO)

    beB = lib.zeros(layer.dimI, dtype=dtype)
    lib.bprop_conv(layer,  beF, beE, beB)

    beU = lib.zeros(layer.dimF, dtype=dtype)
    lib.update_conv(layer, beI, beE, beU)

    return beO, beB, beU


def test_conv_layer():

    dtype = np.float32

    ng = NervanaGPU(stochastic_round=False, bench=True)
    nc = NervanaCPU()

    N, C, K = 64, 64, 64
    D, H, W = 1, 5, 5
    T, R, S = 1, 3, 3
    padding_d, padding_h, padding_w = 0, 1, 1
    strides_d, strides_h, strides_w = 1, 1, 1

    conv_ng = ng.conv_layer(
        dtype,
        N, C, K,
        D, H, W,
        T, R, S,
        padding_d, padding_h, padding_w,
        strides_d, strides_h, strides_w)

    conv_nc = nc.conv_layer(
        dtype,
        N, C, K,
        D, H, W,
        T, R, S,
        padding_d, padding_h, padding_w,
        strides_d, strides_h, strides_w)

    assert conv_nc.dimI == conv_ng.dimI
    assert conv_nc.dimF == conv_ng.dimF
    assert conv_nc.dimO == conv_ng.dimO
    assert conv_nc.M == conv_ng.M

    dimI = conv_ng.dimI
    dimF = conv_ng.dimF
    dimO = conv_ng.dimO

    # cpu input arrays
    cpuI = np.random.uniform(-0.8, 0.8, slicable(dimI, 1)).astype(np.float32)
    cpuF = np.random.uniform(0.0, 0.3, slicable(dimF)).astype(np.float32)
    cpuE = np.random.uniform(-0.2, 0.2, dimO).astype(np.float32)

    # zero pad the last row of cpu input for the sake of numpy
    cpuI[-1, :] = 0.0

    # =======GPU and CPU==========
    beI = cpuI[:-1, :].reshape(dimI)
    beF = cpuF.reshape(dimF)
    beE = cpuE

    start_gpu = default_timer()
    ngO, ngB, ngU = run_backend_conv(ng, conv_ng, beI, beF, beE, dtype)
    end_gpu = default_timer()

    start_cpu = default_timer()
    ncO, ncB, ncU = run_backend_conv(nc, conv_nc, beI, beF, beE, dtype)
    end_cpu = default_timer()

    print ("gputime: %s, cputime %s" %
           (end_gpu - start_gpu, end_cpu - start_cpu))

    # ======numpy===========
    # cpu output arrays
    cpuO = np.zeros(dimO, dtype=dtype)
    cpuB = np.zeros(slicable(dimI, 1), dtype=dtype)
    cpuU = np.zeros(slicable(dimF), dtype=dtype)

    D, H, W = conv_nc.DHW
    T, R, S = conv_nc.TRS
    M, P, Q = conv_nc.MPQ

    pad_d, pad_h, pad_w = conv_nc.padding
    str_d, str_h, str_w = conv_nc.strides

    for m in range(M):
        mt = m * str_d - pad_d

        for p in range(P):
            pr = p * str_h - pad_h

            for q in range(Q):
                qs = q * str_w - pad_w

                idx = pixel_indices(conv_nc, mt, pr, qs)

                cpuO[:, m, p, q, :] = np.dot(cpuF.T, cpuI[idx, :])

                cpuB[idx, :] += np.dot(cpuF, cpuE[:, m, p, q, :])

                cpuU += np.dot(cpuI[idx, :], cpuE[:, m, p, q, :].T)

    for op, ngA, ncA, cpuA, w in (
            ("fprop", ngO, ncO, cpuO, Q),
            ("bprop", ngB, ncB.reshape(dimI), cpuB[:-1, :].reshape(dimI), W),
            ("update", ngU, ncU.reshape(dimF), cpuU.reshape(dimF), S)):

        print op
        assert np.allclose(ngA.get(), cpuA, rtol=0, atol=1e-4)
        assert np.allclose(ncA.get(), cpuA, rtol=0, atol=1e-5)

    ng.ctx.detach()
    del ng
