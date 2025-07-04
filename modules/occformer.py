import torch
import torch.nn as nn
from .base import BaseFormer
from .common import (
    AddNorm, 
    ConvBNorm, 
    ConvTransposeBNorm, 
    PosEmbedding1D, 
    TemporalSpecificMLP, 
    SimpleMLP
)
from .attentions import MultiHeadedAttention
from typing import Tuple, Optional


class OccFormerDecoderLayer(nn.Module):
    def __init__(
            self,
            num_heads: int, 
            embed_dim: int,
            num_ref_points: int,
            dim_feedforward: int=512, 
            dropout: float=0.1,
            offset_scale: float=1.0,
            dense_feature_shape: Tuple[int, int]=(50, 50),
            op_attn_scale: int=2,
        ):
        super(OccFormerDecoderLayer, self).__init__()

        self.num_heads           = num_heads
        self.embed_dim           = embed_dim
        self.num_ref_points      = num_ref_points
        self.dim_feedforward     = dim_feedforward
        self.dropout             = dropout
        self.offset_scale        = offset_scale
        self.dense_feature_shape = dense_feature_shape
        self.op_attn_scale       = op_attn_scale

        self.downsampler         = nn.Upsample(scale_factor=1 / self.op_attn_scale, mode="bilinear")
        self.self_attention      = MultiHeadedAttention(
            num_heads=self.num_heads, 
            embed_dim=self.embed_dim, 
            dropout=self.dropout
        )
        self.addnorm1            = AddNorm(input_dim=self.embed_dim)
        self.cross_attention     = MultiHeadedAttention(
            num_heads=self.num_heads, 
            embed_dim=self.embed_dim, 
            dropout=self.dropout
        )
        self.addnorm2            = AddNorm(input_dim=self.embed_dim)
        self.upsampler           = nn.Upsample(scale_factor=self.op_attn_scale, mode="bilinear")

    def forward(
            self, 
            dense_features: torch.Tensor,
            agent_features: torch.Tensor, 
            mask_features: torch.Tensor,
            return_attn_mask: bool=False,
            pad_mask: Optional[torch.Tensor]=None
        ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:

        """
        Input
        --------------------------------
        :dense_features: (N, embed_dim, (H_bev / s), (W_bev / s)). For the first layer of the OccFormer,
                         this is a bilinear downsampled version of the BEV features, and for subsequent layers, it is
                         the previous layer output at t-1. 

        :agent_features: (N, num_agents, embed_dim). Encoded sparse features from the MotionFormer's 
                          temporal-specific MLP

        :mask_features: (N, num_agents, embed_dim). Encoded agent features from the MotionFormer's 
                        mask features MLP

        :return_attn_mask: if True, returns learned attention mask

        :pad_mask: (N, num_agents), bool padding mask for invalid agent queries (0 if pad value, else 1):

        Returns
        --------------------------------
        output: (N, (C_bev or embed_dim), (H_bev / s), (W_bev / s)) dense features input for next state

        attn_mask: (N, num_agents, (H_bev / 2s), (W_bev / 2s))
        """
        batch_size = dense_features.shape[0]
        ds_shape   = (
            self.dense_feature_shape[0] // self.op_attn_scale, 
            self.dense_feature_shape[1] // self.op_attn_scale
        )

        ds_dense_features = self.downsampler(dense_features)
        ds_dense_features = ds_dense_features.permute(0, 2, 3, 1)
        ds_dense_features = ds_dense_features.reshape(
            batch_size, ds_shape[0]*ds_shape[1], self.embed_dim
        )
        out1 = self.self_attention(ds_dense_features, ds_dense_features, ds_dense_features)
        out2 = self.addnorm1(ds_dense_features, out1)
        mask = torch.matmul(out2, mask_features.permute(0, 2, 1))

        if pad_mask is not None:
            assert pad_mask.ndim == 2 and pad_mask.dtype == torch.bool
            pad_mask = pad_mask[:, None, :]
            mask = torch.masked_fill(mask, ~pad_mask, 0.0)
                
        out3 = self.cross_attention(out2, agent_features, agent_features, attention_mask=mask)
        out4 = self.addnorm2(out2, out3)

        out4 = out4.permute(0, 2, 1)
        out4 = out4.reshape(batch_size, self.embed_dim, *ds_shape)
        out5 = self.upsampler(out4) + dense_features

        if return_attn_mask:
            mask = mask.permute(0, 2, 1)
            mask = mask.reshape(batch_size, -1, *ds_shape)
            return out5, mask
        
        return out5, None


class OccFormer(BaseFormer):
    def __init__(
            self, 
            max_num_agents: int,
            num_heads: int=4,
            embed_dim: int=128,
            num_modes: int=6,
            num_ref_points: int=4,
            pred_horizon: int=5,
            dim_feedforward: int=512,
            dropout: float=0.1,
            offset_scale: float=1.0,
            learnable_pe: bool=True,
            num_tmlp_layers: int=2,
            bev_feature_hw: Tuple[int, int]=(200, 200),
            bev_downsmaple_scale: int=4,
            op_attn_scale : int=2
        ):

        super(OccFormer, self).__init__()

        assert bev_downsmaple_scale > 0

        self.num_heads            = num_heads
        self.embed_dim            = embed_dim
        self.num_layers           = pred_horizon
        self.max_num_agents       = max_num_agents
        self.num_modes            = num_modes
        self.num_ref_points       = num_ref_points
        self.pred_horizon         = pred_horizon
        self.dim_feedforward      = dim_feedforward
        self.dropout              = dropout
        self.offset_scale         = offset_scale
        self.learnable_pe         = learnable_pe
        self.num_tmlp_layers      = num_tmlp_layers
        self.bev_feature_hw       = bev_feature_hw
        self.bev_downsmaple_scale = bev_downsmaple_scale
        self.op_attn_scale        = op_attn_scale

        self.agent_pos_emb      = PosEmbedding1D(
            self.max_num_agents, 
            self.embed_dim, 
            learnable=self.learnable_pe
        )
        self.mode_pool_module   = nn.MaxPool2d(kernel_size=(self.num_modes, 1), stride=1)
        self.temporal_mlp       = TemporalSpecificMLP(
            embed_dim * 3, 
            embed_dim, 
            num_timesteps=self.pred_horizon, 
            hidden_dim=self.dim_feedforward, 
            num_layers=self.num_tmlp_layers
        )
        self.mask_features_mlp  = SimpleMLP(self.embed_dim, self.embed_dim, self.dim_feedforward)
        self.occ_features_mlp   = SimpleMLP(self.embed_dim, self.embed_dim, self.dim_feedforward)

        self.downsampler        = nn.Upsample(scale_factor=1 / self.bev_downsmaple_scale, mode="bilinear")
        self.decoder_modules    = self._create_decoder_layers()
        self.conv_transpose     = nn.Sequential(
            ConvBNorm(embed_dim, embed_dim, kernel_size=1, stride=1, padding=0),
            ConvTransposeBNorm(
                embed_dim, 
                embed_dim,
                kernel_size=self.bev_downsmaple_scale, 
                stride=self.bev_downsmaple_scale, 
                padding=0
            ),
            ConvBNorm(embed_dim, embed_dim, kernel_size=1, stride=1, padding=0),
        )

    def _create_decoder_layers(self) -> nn.ModuleList:
        dense_feature_shape = (
            self.bev_feature_hw[0] // self.bev_downsmaple_scale, 
            self.bev_feature_hw[1] // self.bev_downsmaple_scale
        )
        return nn.ModuleList([
            OccFormerDecoderLayer(
                num_heads=self.num_heads,
                embed_dim=self.embed_dim,
                num_ref_points=self.num_ref_points,
                dim_feedforward=self.dim_feedforward,
                dropout=self.dropout,
                offset_scale=self.offset_scale,
                dense_feature_shape=dense_feature_shape,
                op_attn_scale=self.op_attn_scale

            ) for _ in range(0, self.num_layers)
        ])

    def forward(
            self, 
            bev_features: torch.Tensor, 
            track_queries: torch.Tensor, 
            motion_queries: torch.Tensor,
            pad_mask: Optional[torch.Tensor]=None,
        ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        """
        Input
        --------------------------------
        :bev_features: (N, H_bev * W_bev, (C_bev or embed_dim)) Bird eye view features from BEVFormer

        :track_queries: (N, num_agents, embed_dim) Agent track queries from the TrackFormer

        :motion_queries: (N, num_agents, k, embed_dim), Motion queries from the MotionFormer

        :pad_mask: :pad_mask: (N, num_agents), bool padding mask for invalid agent queries (0 if pad value, else 1):

        Returns
        --------------------------------
        :occupancies: (N, num_agents, T_o, H_bev, W_bev)
            the future occupancy map for multiple agents (where T_o = pred_horizon or num_layers).
                   
        """

        assert track_queries.shape[1] == motion_queries.shape[1]
        assert track_queries.shape[2] == motion_queries.shape[3] and motion_queries.shape[3] == self.embed_dim
        assert motion_queries.shape[2] == self.num_modes
        assert bev_features.shape[2] == self.embed_dim

        device = track_queries.device
        batch_size, num_agents = track_queries.shape[:2]
        
        x_queries       = self.mode_pool_module(motion_queries)
        x_queries       = x_queries[..., 0, :]

        pos_indexes     = torch.arange(num_agents, device=device)[None, :].tile(batch_size, 1)
        pos_emb         = self.agent_pos_emb(pos_indexes)
        sparse_features = torch.concat([track_queries, pos_emb, x_queries], dim=-1)

        bev_features    = bev_features.permute(0, 2, 1)
        bev_features    = bev_features.reshape(batch_size, self.embed_dim, *self.bev_feature_hw)
        dense_features  = self.downsampler(bev_features)

        occupancies     = []
        all_attn_masks  = []

        for tidx in range(0, self.num_layers):
            agent_features  = self.temporal_mlp(sparse_features, tidx)
            mask_features   = self.mask_features_mlp(agent_features)

            # each layer corresponds to a timestep, and for each timestep, the estimated occupancy of that and
            # the previous timsteps are accumulated into oen final accumulated agent future occupancy map, 
            # ideally, the last estimated occupancy is the most complete occupancy, however other occupancies
            # at prior timesteps are not useless, they still need to be compared with their corresponding groud
            # truths for the sake of coherence and consistency.
            dense_features, attn_masks = self.decoder_modules[tidx](
                dense_features=dense_features,
                agent_features=agent_features,
                mask_features=mask_features,
                return_attn_mask = (not self.inference_mode),
                pad_mask=pad_mask
            )
            if attn_masks is not None:
                all_attn_masks.append(attn_masks)
            
            occ_features = self.occ_features_mlp(mask_features)
            proba_map    = self.conv_transpose(dense_features)
            proba_map    = proba_map.permute(0, 2, 1, 3).reshape(batch_size, -1, self.embed_dim)
            occupancy    = torch.matmul(occ_features, proba_map.permute(0, 2, 1))
            occupancy    = occupancy.permute(0, 2, 1)
            occupancy    = occupancy.reshape(batch_size, num_agents, *self.bev_feature_hw)
            occupancies.append(occupancy)

        occupancies = torch.stack(occupancies, dim=2)

        if len(all_attn_masks) > 0:
            all_attn_masks = torch.stack(all_attn_masks, dim=2)
            return occupancies, all_attn_masks

        return occupancies, None