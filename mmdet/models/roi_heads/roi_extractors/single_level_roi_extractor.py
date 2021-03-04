import torch
from mmcv.runner import force_fp32

from mmdet.core.utils.misc import topk
from mmdet.models.builder import ROI_EXTRACTORS
from mmdet.utils.deployment.symbolic import py_symbolic
from .base_roi_extractor import BaseRoIExtractor


def adapter(self, feats, rois):
    return ((rois,) + tuple(feats), 
        {'output_size': self.roi_layers[0].output_size[0],
         'featmap_strides': self.featmap_strides,
         'sample_num': self.roi_layers[0].sampling_ratio})


@ROI_EXTRACTORS.register_module()
class SingleRoIExtractor(BaseRoIExtractor):
    """Extract RoI features from a single level feature map.

    If there are multiple input feature levels, each RoI is mapped to a level
    according to its scale. The mapping rule is proposed in
    `FPN <https://arxiv.org/abs/1612.03144>`_.

    Args:
        roi_layer (dict): Specify RoI layer type and arguments.
        out_channels (int): Output channels of RoI layers.
        featmap_strides (int): Strides of input feature maps.
        finest_scale (int): Scale threshold of mapping to level 0. Default: 56.
    """

    def __init__(self,
                 roi_layer,
                 out_channels,
                 featmap_strides,
                 finest_scale=56):
        super(SingleRoIExtractor, self).__init__(roi_layer, out_channels,
                                                 featmap_strides)
        self.finest_scale = finest_scale

    def map_roi_levels(self, rois, num_levels):
        """Map rois to corresponding feature levels by scales.

        - scale < finest_scale * 2: level 0
        - finest_scale * 2 <= scale < finest_scale * 4: level 1
        - finest_scale * 4 <= scale < finest_scale * 8: level 2
        - scale >= finest_scale * 8: level 3

        Args:
            rois (Tensor): Input RoIs, shape (k, 5).
            num_levels (int): Total level number.

        Returns:
            Tensor: Level index (0-based) of each RoI, shape (k, )
        """
        scale = torch.sqrt(
            (rois[:, 3] - rois[:, 1]) * (rois[:, 4] - rois[:, 2]))
        target_lvls = torch.floor(torch.log2(scale / self.finest_scale + 1e-6))
        target_lvls = target_lvls.clamp(min=0, max=num_levels - 1).long()
        return target_lvls

    @py_symbolic(op_name='roi_feature_extractor', adapter=adapter)
    @force_fp32(apply_to=('feats', ), out_fp16=True)
    def forward(self, feats, rois, roi_scale_factor=None):
        from torch.onnx import operators

        if len(feats) == 1:
            return self.roi_layers[0](feats[0], rois)

        num_levels = len(feats)
        target_lvls = self.map_roi_levels(rois, num_levels)
        if roi_scale_factor is not None:
            rois = self.roi_rescale(rois, roi_scale_factor)

        indices = []
        roi_feats = []
        for level, (feat, extractor) in enumerate(zip(feats, self.roi_layers)):
            # Explicit casting to int is required for ONNXRuntime.
            level_indices = torch.nonzero(
                (target_lvls == level).int()).view(-1)
            level_rois = rois[level_indices]
            indices.append(level_indices)
            try:
                level_feats = extractor(feat, level_rois)
                roi_feats.append(level_feats)
            except:
                pass
        # Concatenate roi features from different pyramid levels
        # and rearrange them to match original ROIs order.
        indices = torch.cat(indices, dim=0)
        k = operators.shape_as_tensor(indices)
        _, indices = topk(indices, k, dim=0, largest=False)
        roi_feats = torch.cat(roi_feats, dim=0)[indices]

        return roi_feats
