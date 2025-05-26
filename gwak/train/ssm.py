import os
import time
import yaml
import logging
import h5py
from typing import Sequence
from collections import OrderedDict
from sklearn.model_selection import train_test_split

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import lightning.pytorch as pl

import math
from typing import Optional, Union

import torch
import torch.nn as nn
from einops import rearrange, repeat
from gwak.train.losses import SupervisedSimCLRLoss
from gwak.train.schedulers import WarmupCosineAnnealingLR
from gwak.train.plotting import make_corner

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import wandb
from PIL import Image
from io import BytesIO
import shutil

class DropoutNd(nn.Module):
    def __init__(self, p: float = 0.5, tie=True, transposed=True):
        """
        tie: tie dropout mask across sequence lengths (Dropout1d/2d/3d)
        """
        super().__init__()
        if p < 0 or p >= 1:
            raise ValueError(
                "dropout probability has to be in [0, 1), "
                "but got {}".format(p)
            )
        self.p = p
        self.tie = tie
        self.transposed = transposed
        self.binomial = torch.distributions.binomial.Binomial(probs=1 - self.p)

    def forward(self, X):
        """X: (batch, dim, lengths...)."""
        if self.training:
            if not self.transposed:
                X = rearrange(X, "b ... d -> b d ...")
            mask_shape = (
                X.shape[:2] + (1,) * (X.ndim - 2) if self.tie else X.shape
            )
            # mask = self.binomial.sample(mask_shape)
            mask = torch.rand(*mask_shape, device=X.device) < 1.0 - self.p
            X = X * mask * (1.0 / (1 - self.p))
            if not self.transposed:
                X = rearrange(X, "b d ... -> b ... d")
            return X
        return X

class S4DKernel(nn.Module):
    """Generate convolution kernel from diagonal SSM parameters."""

    def __init__(
        self,
        d_model: int,
        length: int,
        N: int = 64,
        dt_min: float = 0.001,
        dt_max: float = 0.1,
        lr: float = None,
    ):
        super().__init__()

        # generate dt
        H = d_model
        log_dt = torch.rand(H) * (
            math.log(dt_max) - math.log(dt_min)
        ) + math.log(dt_min)

        C = torch.randn(H, N // 2, dtype=torch.cfloat)
        self.C = nn.Parameter(torch.view_as_real(C))
        self.register("log_dt", log_dt, lr)

        log_A_real = torch.log(0.5 * torch.ones(H, N // 2))
        A_imag = math.pi * repeat(torch.arange(N // 2), "n -> h n", h=H)
        self.register("log_A_real", log_A_real, lr)
        self.register("A_imag", A_imag, lr)

        Ls = torch.arange(length)
        self.register_buffer("length", Ls)

    def forward(self):
        """
        returns: (..., c, L) where c is number of channels (default 1)
        """

        # Materialize parameters
        dt = torch.exp(self.log_dt)  # (H)
        C = torch.view_as_complex(self.C)  # (H N)
        A = -torch.exp(self.log_A_real) + 1j * self.A_imag  # (H N)

        # Vandermonde multiplication
        dtA = A * dt.unsqueeze(-1)  # (H N)
        K = dtA.unsqueeze(-1) * self.length  # (H N L)
        C = C * (torch.exp(dtA) - 1.0) / A
        K = 2 * torch.einsum("hn, hnl -> hl", C, torch.exp(K)).real

        return K

    def register(self, name, tensor, lr=None):
        """
        Register a tensor with a configurable learning rate
        and 0 weight decay
        """

        if lr == 0.0:
            self.register_buffer(name, tensor)
        else:
            self.register_parameter(name, nn.Parameter(tensor))

            optim = {"weight_decay": 0.0}
            if lr is not None:
                optim["lr"] = lr
            setattr(getattr(self, name), "_optim", optim)

class S4D(nn.Module):
    def __init__(
        self,
        d_model: int,
        length: int,
        d_state: int = 64,
        dropout: float = 0.0,
        transposed: bool = True,
        dt_min: float = 0.001,
        dt_max: float = 0.1,
        lr: Optional[float] = None,
    ):
        super().__init__()
        self.transposed = transposed
        self.D = nn.Parameter(torch.randn(d_model))
        self.length = length

        # SSM Kernel
        self.kernel = S4DKernel(
            d_model,
            length=length,
            N=d_state,
            dt_min=dt_min,
            dt_max=dt_max,
            lr=lr,
        )

        # Pointwise
        self.activation = nn.GELU()
        # TODO: investigate torch dropout implementation
        self.dropout = torch.nn.Dropout1d(dropout)
        # self.dropout = DropoutNd(dropout) if dropout > 0.0 else nn.Identity()

        # position-wise output transform to mix features
        self.output_linear = nn.Sequential(
            nn.Conv1d(d_model, 2 * d_model, kernel_size=1),
            nn.GLU(dim=-2),
        )

    def forward(self, u):
        """Input and output shape (B, H, L)"""
        if not self.transposed:
            u = u.transpose(-1, -2)

        # Compute SSM Kernel
        k = self.kernel()  # (H L)

        # Convolution
        k_f = torch.fft.rfft(k, n=2 * self.length)  # (H L)
        u_f = torch.fft.rfft(u, n=2 * self.length)  # (B H L)
        y = torch.fft.irfft(u_f * k_f, n=2 * self.length)[
            ..., : self.length
        ]  # (B H L)

        # Compute D term in state space equation
        # Essentially a skip connection
        y = y + u * self.D.unsqueeze(-1)

        y = self.dropout(self.activation(y))
        y = self.output_linear(y)
        if not self.transposed:
            y = y.transpose(-1, -2)
        # Return a dummy state to satisfy this repo's interface,
        # but this can be modified
        return y, None

class S4Model(nn.Module):
    def __init__(
        self,
        d_input: int,
        length: int,
        d_output: int = 10,
        d_model: int = 256,
        d_state: int = 64,
        n_layers: int = 4,
        dropout: float = 0.2,
        prenorm: bool = False,
        dt_min: float = 0.001,
        dt_max: float = 0.1,
        lr: Optional[float] = None
    ):
        super().__init__()

        self.prenorm = prenorm

        # Linear encoder (d_input = 1 for grayscale and 3 for RGB)
        self.encoder = nn.Linear(d_input, d_model)

        # Stack S4 layers as residual blocks
        self.s4_layers = nn.ModuleList()
        self.norms = nn.ModuleList()
        self.dropouts = nn.ModuleList()
        if lr is not None:
            lr = min(0.001, lr)
        for _ in range(n_layers):
            self.s4_layers.append(
                S4D(
                    length=length,
                    d_model=d_model,
                    d_state=d_state,
                    dropout=dropout,
                    transposed=True,
                    dt_min=dt_min,
                    dt_max=dt_max,
                    lr=lr,
                )
            )
            self.norms.append(nn.LayerNorm(d_model))
            self.dropouts.append(nn.Dropout1d(dropout))

        # Linear decoder
        self.decoder = nn.Linear(d_model, d_output)

    def forward(self, x):
        """
        Input x is shape (B, d_input, L)
        """
        x = x.transpose(-1, -2)
        x = self.encoder(x)  # (B, L, d_input) -> (B, L, d_model)

        x = x.transpose(-1, -2)  # (B, L, d_model) -> (B, d_model, L)
        for layer, norm, dropout in zip(
            self.s4_layers, self.norms, self.dropouts
        ):
            # Each iteration of this loop will map
            # (B, d_model, L) -> (B, d_model, L)

            z = x
            if self.prenorm:
                # Prenorm
                z = norm(z.transpose(-1, -2)).transpose(-1, -2)

            # Apply S4 block: we ignore the state input and output
            z, _ = layer(z)

            # Dropout on the output of the S4 block
            z = dropout(z)

            # Residual connection
            x = z + x

            if not self.prenorm:
                # Postnorm
                x = norm(x.transpose(-1, -2)).transpose(-1, -2)

        x = x.transpose(-1, -2)

        # Pooling: average pooling over the sequence length
        x = x.mean(dim=1)

        # Decode the outputs
        x = self.decoder(x)  # (B, d_model) -> (B, d_output)

        return x