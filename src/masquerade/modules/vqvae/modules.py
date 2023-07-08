from os import PathLike
from typing import Optional, Sequence

import numpy as np
import torch
from torch import Tensor, nn

from masquerade.modules.vqvae.layers import Conv2dSame, DownEncoderBlock2D, ResBlock, UpDecoderBlock2D


class ConvEncoder(nn.Module):
    """Convolutional encoder for VQ-VAE.

    This is a cut-down version of diffusers.models.vae.Encoder with the non-local block removed,
    and assorted other changes to match the fully-convolutional architecture of MaskGit's VAE.
    """

    def __init__(
        self,
        in_channels: int = 3,
        out_channels: int = 256,
        block_out_channels: Sequence[int] = (128, 128, 256, 256, 512),
        layers_per_block: int = 2,
        norm_num_groups: int = 32,
        use_conv_shortcut: bool = False,
        conv_downsample: bool = False,
        double_z: bool = False,
    ):
        super().__init__()
        self.layers_per_block = layers_per_block
        self.in_channels = in_channels
        self.out_channels = 2 * out_channels if double_z else out_channels

        self.conv_in = Conv2dSame(self.in_channels, block_out_channels[0], kernel_size=3, bias=False)

        # down
        self.down_blocks = nn.ModuleList([])
        output_channel = block_out_channels[0]
        num_blocks = len(block_out_channels)
        for i in range(num_blocks):
            prev_output_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == num_blocks - 1

            down_block = DownEncoderBlock2D(
                in_channels=prev_output_channel,
                out_channels=output_channel,
                num_layers=self.layers_per_block,
                resnet_eps=1e-6,
                resnet_groups=norm_num_groups,
                use_conv_shortcut=use_conv_shortcut,
                add_downsample=not is_final_block,
                conv_downsample=conv_downsample,
            )
            self.down_blocks.append(down_block)

        # mid
        self.res_blocks = nn.ModuleList([])
        for _ in range(layers_per_block):
            self.res_blocks.append(ResBlock(block_out_channels[-1], groups=norm_num_groups))

        # out
        self.norm_out = nn.GroupNorm(
            num_channels=block_out_channels[-1], num_groups=norm_num_groups, eps=1e-6
        )
        self.act_out = nn.SiLU()
        self.conv_out = Conv2dSame(block_out_channels[-1], self.out_channels, kernel_size=1)

    def forward(self, x: torch.Tensor):
        # down
        x = self.conv_in(x)
        for down_block in self.down_blocks:
            x = down_block(x)

        # mid
        for res_block in self.res_blocks:
            x = res_block(x)

        # end
        x = self.norm_out(x)
        x = self.act_out(x)
        x = self.conv_out(x)
        return x

    def get_last_layer(self):
        return self.conv_out.weight


class ConvDecoder(nn.Module):
    """Convolutional decoder for VQ-VAE.

    This is a copy of diffusers.models.vae.Decoder with the non-local block removed,
    and the default arguments changed to match the defaults in the MaskGit paper.

    This shares arguments with the encoder, so the channel count list is reversed!
    """

    def __init__(
        self,
        in_channels: int = 3,
        out_channels: int = 256,
        block_out_channels: Sequence[int] = (128, 128, 256, 256, 512),
        layers_per_block: int = 2,
        norm_num_groups: int = 32,
        use_conv_shortcut: bool = False,
        conv_downsample: bool = False,  # shared with encoder, unused here
        double_z: bool = False,
    ):
        super().__init__()
        self.layers_per_block = layers_per_block

        # flip the channel counts since we're sharing args with the encoder
        self.in_channels = 2 * out_channels if double_z else out_channels
        self.out_channels = in_channels
        block_out_channels = list(reversed(block_out_channels))

        # in
        self.conv_in = Conv2dSame(self.in_channels, block_out_channels[0], kernel_size=3, bias=True)

        # mid
        self.res_blocks = nn.ModuleList([])
        for _ in range(layers_per_block):
            self.res_blocks.append(
                ResBlock(
                    block_out_channels[0],
                    groups=norm_num_groups,
                    eps=1e-6,
                    use_conv_shortcut=use_conv_shortcut,
                )
            )

        # up
        self.up_blocks = nn.ModuleList([])
        output_channel = block_out_channels[0]
        num_blocks = len(block_out_channels)
        for i in range(num_blocks):
            prev_output_channel = output_channel
            output_channel = block_out_channels[i]
            is_first_block = i == 0

            up_block = UpDecoderBlock2D(
                in_channels=prev_output_channel,
                out_channels=output_channel,
                num_layers=self.layers_per_block,
                resnet_eps=1e-6,
                resnet_groups=norm_num_groups,
                use_conv_shortcut=use_conv_shortcut,
                add_upsample=is_first_block,
            )
            self.up_blocks.append(up_block)

        # out
        self.norm_out = nn.GroupNorm(
            num_channels=block_out_channels[-1], num_groups=norm_num_groups, eps=1e-6
        )
        self.act_out = nn.SiLU()
        self.conv_out = Conv2dSame(block_out_channels[-1], self.out_channels, 3)

    def forward(self, x: Tensor) -> Tensor:
        # in
        x = self.conv_in(x)

        # mid
        for res_block in self.res_blocks:
            x = res_block(x)

        # up
        for up_block in self.up_blocks:
            x = up_block(x)

        # out
        x = self.norm_out(x)
        x = self.act_out(x)
        x = self.conv_out(x)
        return x

    def get_last_layer(self) -> Tensor:
        return self.conv_out.weight


class VectorQuantize2(nn.Module):
    """
    Improved version over VectorQuantizer, can be used as a drop-in replacement. Mostly avoids costly matrix
    multiplications and allows for post-hoc remapping of indices.
    """

    used: Optional[Tensor]  # used for remapping indices

    def __init__(
        self,
        n_e: int,
        vq_embed_dim: int,
        beta: float,
        remap: Optional[PathLike] = None,
        unknown_index: str = "random",
        sane_index_shape: bool = False,
    ):
        super().__init__()
        self.n_e = n_e
        self.vq_embed_dim = vq_embed_dim
        self.beta = beta

        self.embedding = nn.Embedding(self.n_e, self.vq_embed_dim)
        self.embedding.weight.data.uniform_(-1.0 / self.n_e, 1.0 / self.n_e)

        self.remap = remap
        if self.remap is not None:
            self.register_buffer("used", torch.tensor(np.load(self.remap)))
            self.re_embed = self.used.shape[0]
            self.unknown_index = unknown_index  # "random" or "extra" or integer
            if self.unknown_index == "extra":
                self.unknown_index = self.re_embed
                self.re_embed = self.re_embed + 1
            print(
                f"Remapping {self.n_e} indices to {self.re_embed} indices. "
                f"Using {self.unknown_index} for unknown indices."
            )
        else:
            self.re_embed = n_e

        self.sane_index_shape = sane_index_shape

    def remap_to_used(self, inds: Tensor) -> Tensor:
        ishape = inds.shape
        assert len(ishape) > 1
        inds = inds.reshape(ishape[0], -1)
        used = self.used.to(inds)
        match = (inds[:, :, None] == used[None, None, ...]).long()
        new = match.argmax(-1)
        unknown = match.sum(2) < 1
        if self.unknown_index == "random":
            new[unknown] = torch.randint(0, self.re_embed, size=new[unknown].shape).to(device=new.device)
        else:
            new[unknown] = self.unknown_index
        return new.reshape(ishape)

    def unmap_to_all(self, inds):
        ishape = inds.shape
        assert len(ishape) > 1
        inds = inds.reshape(ishape[0], -1)
        used = self.used.to(inds)
        if self.re_embed > self.used.shape[0]:  # extra token
            inds[inds >= self.used.shape[0]] = 0  # simply set to zero
        back = torch.gather(used[None, :][inds.shape[0] * [0], :], 1, inds)
        return back.reshape(ishape)

    def forward(self, z: Tensor):
        # reshape z -> (batch, height, width, channel) and flatten
        z = z.permute(0, 2, 3, 1).contiguous()
        z_flattened = z.view(-1, self.vq_embed_dim)

        # distances from z to embeddings e_j (z - e)^2 = z^2 + e^2 - 2 e * z
        min_encoding_indices = torch.argmin(torch.cdist(z_flattened, self.embedding.weight), dim=1)

        z_q: torch.Tensor = self.embedding(min_encoding_indices).view(z.shape)
        perplexity = None
        min_encodings = None

        # compute loss for embedding
        loss = self.beta * torch.mean((z_q.detach() - z) ** 2) + torch.mean((z_q - z.detach()) ** 2)

        # preserve gradients
        z_q = z + (z_q - z).detach()

        # reshape back to match original input shape
        z_q = z_q.permute(0, 3, 1, 2).contiguous()

        if self.remap is not None:
            min_encoding_indices = min_encoding_indices.reshape(z.shape[0], -1)  # add batch axis
            min_encoding_indices = self.remap_to_used(min_encoding_indices)
            min_encoding_indices = min_encoding_indices.reshape(-1, 1)  # flatten

        if self.sane_index_shape:
            min_encoding_indices = min_encoding_indices.reshape(z_q.shape[0], z_q.shape[2], z_q.shape[3])

        return z_q, loss, (perplexity, min_encodings, min_encoding_indices)

    def get_codebook_entry(self, indices: Tensor, shape):
        # shape specifying (batch, height, width, channel)
        if self.remap is not None:
            indices = indices.reshape(shape[0], -1)  # add batch axis
            indices = self.unmap_to_all(indices)
            indices = indices.reshape(-1)  # flatten again

        # get quantized latent vectors
        z_q = self.embedding(indices)

        if shape is not None:
            z_q = z_q.view(shape)
            # reshape back to match original input shape
            z_q = z_q.permute(0, 3, 1, 2).contiguous()

        return z_q
