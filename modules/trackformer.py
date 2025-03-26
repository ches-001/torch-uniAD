import torch
import torch.nn as nn
import torch.nn.functional as F
from .attentions import MultiHeadedAttention, DeformableAttention
from .common import AddNorm, PosEmbedding1D, DetectionHead
from typing import *


class TrackFormerDecoderLayer(nn.Module):
    def __init__(
        self,
        num_heads: int, 
        embed_dim: int,
        num_ref_points: int=4,
        dim_feedforward: int=512, 
        dropout: float=0.1,
        offset_scale: float=1.0,
        bev_feature_shape: Tuple[int, int]=(200, 200),
    ):
        super(TrackFormerDecoderLayer, self).__init__()

        self.num_heads         = num_heads
        self.embed_dim         = embed_dim
        self.num_ref_points    = num_ref_points
        self.dim_feedforward   = dim_feedforward
        self.dropout           = dropout
        self.offset_scale      = offset_scale
        self.bev_feature_shape = bev_feature_shape

        self.self_attention      = MultiHeadedAttention(
            self.num_heads, 
            self.embed_dim, 
            dropout=self.dropout, 
        )
        self.addnorm1            = AddNorm(input_dim=self.embed_dim)
        self.deform_attention    = DeformableAttention(
            self.num_heads,
            self.embed_dim, 
            num_ref_points=num_ref_points, 
            dropout=self.dropout, 
            offset_scale=self.offset_scale,
            num_fmap_levels=1,
            concat_vq_for_offset=False,
        )
        self.addnorm2            = AddNorm(input_dim=self.embed_dim)
        self.mlp                 = nn.Sequential(
            nn.Linear(self.embed_dim, self.dim_feedforward),
            nn.ReLU(),
            nn.Linear(self.dim_feedforward, self.embed_dim),
        )
        self.addnorm3            = AddNorm(input_dim=self.embed_dim)

    def forward(
            self, 
            queries: torch.Tensor,
            bev_features: torch.Tensor,
            ref_points: torch.Tensor, 
            og_det_queries: torch.Tensor,
            padding_mask: torch.Tensor,
        ) -> torch.Tensor:
        """
        Input
        --------------------------------

        :queries: (N, num_queries, det_embeds) input queries (num_queries = num_detections + num_track)

        :bev_features: (N, W_bev * H_bev, (C_bev or embed_dim))

        :ref_points: (N, num_queries, 1, 2), reference points for the deformable attention

        :og_det_queries: (N, num_detections, det_embeds), original object / detection queries

        :padding_mask: (N, num_queries), padding / attention mask for queries (0 if to ignore else 1)

        Returns
        --------------------------------
        :track_queries: (N, num_queries, embed_dim), output queries to be fed into the next layer
        """
        H_bev, W_bev = self.bev_feature_shape
        assert bev_features.shape[1] == H_bev * W_bev
        assert bev_features.shape[2] == queries.shape[2] and bev_features.shape[2] == self.embed_dim

        bev_spatial_shape = torch.LongTensor([[H_bev, W_bev]], device=queries.device)
        num_queries       = queries.shape[1]
        num_det           = og_det_queries.shape[1]
        num_tracks        = num_queries - num_det
        q_and_k           = queries
        
        # i could have easily done queries[:, o_queries.shape[1]: :] += o_queries or so, but inplace operations
        # on tensors with gradient history screw up gradients and causes inplace errors
        if num_tracks > 0:
            q_and_k = [q_and_k[:, :num_tracks, :], q_and_k[:, num_tracks:, :] + og_det_queries]
            q_and_k = torch.concat(q_and_k, dim=1)
        else:
            q_and_k = q_and_k + og_det_queries

        out1     = self.self_attention(q_and_k, q_and_k, queries, padding_mask=padding_mask)
        out2     = self.addnorm1(queries, out1)
        aug_out2 = out2

        if num_tracks > 0:
            aug_out2 = [out2[:, :num_tracks, :], out2[:, num_tracks:, :] + og_det_queries]
            aug_out2 = torch.concat(aug_out2, dim=1)
        else:
            aug_out2 = aug_out2 + og_det_queries

        out3 = self.deform_attention(
            aug_out2, ref_points, bev_features, bev_spatial_shape, attention_mask=padding_mask[..., None]
        )
        out4 = self.addnorm2(out2, out3)
        out5 = self.mlp(out4)
        out6 = self.addnorm3(out4, out5)
        return out6


class TrackFormer(nn.Module):
    def __init__(
            self,
            num_heads: int, 
            embed_dim: int,
            num_layers: int,
            num_classes: int,
            num_ref_points: int=4,
            dim_feedforward: int=512, 
            dropout: float=0.1,
            offset_scale: float=1.0,
            max_detections: int=900,
            learnable_pe: bool=True,
            bev_feature_shape: Tuple[int, int]=(200, 200),
            track_threshold: float=0.5,
            det_3d: bool=True
        ):
        super(TrackFormer, self).__init__()

        self.num_heads         = num_heads
        self.embed_dim         = embed_dim
        self.num_layers        = num_layers
        self.num_classes       = num_classes
        self.num_ref_points    = num_ref_points
        self.dim_feedforward   = dim_feedforward
        self.dropout           = dropout
        self.offset_scale      = offset_scale
        self.max_detections    = max_detections
        self.learnable_pe      = learnable_pe
        self.bev_feature_shape = bev_feature_shape
        self.track_threshold   = track_threshold
        self.det_3d            = det_3d

        self.detection_pos_emb  = PosEmbedding1D(
            self.max_detections, 
            embed_dim=self.embed_dim, 
            learnable=learnable_pe
        )
        self.decoder_modules    = self._create_decoder_layers()
        self.detection_module   = DetectionHead(
            embed_dim=self.embed_dim, 
            num_classes=self.num_classes, 
            det_3d=self.det_3d,
            num_seg_coefs=None
        )

    def _create_decoder_layers(self) -> nn.ModuleList:
        return nn.ModuleList([TrackFormerDecoderLayer(
            num_heads=self.num_heads, 
            embed_dim=self.embed_dim, 
            num_ref_points=self.num_ref_points, 
            dim_feedforward=self.dim_feedforward,
            dropout=self.dropout,
            offset_scale=self.offset_scale,
            bev_feature_shape=self.bev_feature_shape
        ) for _ in range(self.num_layers)])
    

    def forward(
            self, 
            bev_features: torch.Tensor, 
            track_queries: Optional[torch.Tensor]=None,
            track_queries_mask: Optional[torch.BoolTensor]=None,
        ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]:

        """
        Input
        --------------------------------
        :bev_features: (N, H_bev*W_bev, (C_bev or embed_dim)), BEV features from the BevFormer encoder

        :track_queries: (N, num_queries, embed_dim), embedding output of TrackFormer decoder at previous timestep (t-1)

        :track_queries_mask: (N, num_queries), boolean mask, indicating the tracks with valid and invalid detections
                            NOTE: Detection is invalid if score <= track_threshold (1 if valid, else 0)

        Returns
        --------------------------------
        if training:
            :output: (N, num_queries, embed_dim) batch of output context query for each segmented item
                    (including invalid detections). NOTE: num_queries = num_detections (max_detections) + num_track. 
                    num_track is the number of track_queries and it is dynamic as it depends on the number of valid.
                    detections

            :layers_detections: (num_layers, N, num_queries, embed_dim), output context query of each layer

        else:
            :detections: (N, num_queries, det_params) batch of detection for multiple identified items
        """
        assert bev_features.shape[-1] == self.embed_dim

        batch_size        = bev_features.shape[0]
        device            = bev_features.device
        detection_queries = self.detection_pos_emb()
        detection_queries = detection_queries.tile(batch_size, 1, 1)
        padding_mask      = torch.ones(*detection_queries.shape[:-1], device=device, dtype=torch.bool)
        
        if track_queries is not None:
            # if track queries are available, combine (concatenate) them with the static detection queries
            # the detection queries are responsible for detecting new objects that enter the frame, the 
            # track queries on the other hand are queries used to persist a detection across multiple frames
            # for as long as said detection is alive, inotherwords, tracking. Since each sample per batch can
            # have varying number of valid tracks, for each sample, we retrieve the valid tracks, pad it to a
            # fixed size equivalent to the max number of valid tracks in the batch, then recompute its corresponding
            # mask accordingly.
            assert track_queries.shape[-1] == self.embed_dim
            assert track_queries.shape[1] == track_queries_mask.shape[1]
            max_num_tracks = track_queries_mask.sum(dim=1).max()

            new_track_queries      = []
            new_track_queries_mask = []
            for i in range(0, batch_size):
                tracks                         = track_queries[i][track_queries_mask[i]]
                num_valid_tracks               = tracks.shape[0]
                pad_size                       = max_num_tracks - num_valid_tracks
                tracks                         = F.pad(tracks, pad=(0, 0, 0, pad_size), mode="constant", value=0)
                tracks_mask                    = torch.zeros(max_num_tracks, device=device, dtype=torch.bool)
                tracks_mask[:num_valid_tracks] = True
                new_track_queries.append(tracks)
                new_track_queries_mask.append(tracks_mask)

            track_queries      = torch.stack(new_track_queries, dim=0)
            track_queries_mask = torch.stack(new_track_queries_mask, dim=0)
            queries            = torch.concat([track_queries, detection_queries], dim=1)
            padding_mask       = torch.concat([track_queries_mask, padding_mask], dim=1)
        else:
            queries = detection_queries
        
        ref_points        = DeformableAttention.generate_standard_ref_points(
            self.bev_feature_shape,
            batch_size=batch_size, 
            device=bev_features.device, 
            normalize=False, 
            n_sample=queries.shape[1]
        ).unsqueeze(dim=-2)

        layers_detections = []
        for decoder_idx in range(0, len(self.decoder_modules)):
            output = self.decoder_modules[decoder_idx](
                queries=queries,
                bev_features=bev_features,
                ref_points=ref_points,
                og_det_queries=detection_queries,
                padding_mask=padding_mask
            )
            if not self.training:
                if decoder_idx == self.num_layers - 1:
                    return self.detection_module(output)
            else:
                detections = self.detection_module(output)
                layers_detections.append(detections)
            
        layers_detections = torch.stack(layers_detections, dim=0)
        return output, layers_detections