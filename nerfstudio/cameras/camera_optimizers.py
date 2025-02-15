# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Pose and Intrinsics Optimizers
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Literal, Optional, Type, Union

import torch
import tyro
from jaxtyping import Float, Int
from torch import Tensor, nn
from typing_extensions import assert_never

from nerfstudio.cameras.cameras import Cameras
from nerfstudio.cameras.lie_groups import exp_map_SE3, exp_map_SO3xR3
from nerfstudio.cameras.rays import RayBundle
from nerfstudio.configs.base_config import InstantiateConfig
from nerfstudio.engine.optimizers import OptimizerConfig
from nerfstudio.engine.schedulers import SchedulerConfig
from nerfstudio.utils import poses as pose_utils


@dataclass
class CameraOptimizerConfig(InstantiateConfig):
    """Configuration of optimization for camera poses."""

    _target: Type = field(default_factory=lambda: CameraOptimizer)

    mode: Literal["off", "SO3xR3", "SE3", "shared_SO3xR3"] = "off"
    """Pose optimization strategy to use. If enabled, we recommend SO3xR3."""

    trans_l2_penalty: float = 1e-2
    """L2 penalty on translation parameters."""

    rot_l2_penalty: float = 1e-3
    """L2 penalty on rotation parameters."""

    penalty_scale: float = 1
    """Multiplier scaling translation + rotation penalties. If -1, mode = off."""

    # tyro.conf.Suppress prevents us from creating CLI arguments for these fields.
    optimizer: tyro.conf.Suppress[Optional[OptimizerConfig]] = field(default=None)
    """Deprecated, now specified inside the optimizers dict"""

    scheduler: tyro.conf.Suppress[Optional[SchedulerConfig]] = field(default=None)
    """Deprecated, now specified inside the optimizers dict"""

    def __post_init__(self):
        if self.optimizer is not None:
            import warnings

            from nerfstudio.utils.rich_utils import CONSOLE

            CONSOLE.print(
                "\noptimizer is no longer specified in the CameraOptimizerConfig, it is now defined with the rest of the param groups inside the config file under the name 'camera_opt'\n",
                style="bold yellow",
            )
            warnings.warn("above message coming from", FutureWarning, stacklevel=3)

        if self.scheduler is not None:
            import warnings

            from nerfstudio.utils.rich_utils import CONSOLE

            CONSOLE.print(
                "\nscheduler is no longer specified in the CameraOptimizerConfig, it is now defined with the rest of the param groups inside the config file under the name 'camera_opt'\n",
                style="bold yellow",
            )
            warnings.warn("above message coming from", FutureWarning, stacklevel=3)


class CameraOptimizer(nn.Module):
    """Layer that modifies camera poses to be optimized as well as the field during training."""

    config: CameraOptimizerConfig

    def __init__(
        self,
        config: CameraOptimizerConfig,
        num_cameras: int,
        device: Union[torch.device, str],
        non_trainable_camera_indices: Optional[Int[Tensor, "num_non_trainable_cameras"]] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self.config = config
        self.num_cameras = num_cameras
        self.device = device
        self.non_trainable_camera_indices = non_trainable_camera_indices

        if self.config.penalty_scale < 0:
            self.config.mode = "off"

        # Initialize learnable parameters.
        if self.config.mode == "off":
            pass
        elif self.config.mode in ("SO3xR3", "SE3"):
            self.pose_adjustment = torch.nn.Parameter(torch.zeros((num_cameras, 6), device=device))
        elif self.config.mode == "shared_SO3xR3":
            self.pose_adjustment = torch.nn.Parameter(torch.zeros((1, 6), device=device))
        else:
            assert_never(self.config.mode)

        self.suffix = kwargs.get("suffix", "")
        self.non_trainable_camera_filter = None
        if self.non_trainable_camera_indices is not None:
            self.non_trainable_camera_filter = torch.tensor(
                [True if i in self.non_trainable_camera_indices else False for i in range(self.num_cameras)],
                dtype=torch.bool,
                device=device,
            )

    def forward(
        self,
        indices: Int[Tensor, "camera_indices"],
    ) -> Float[Tensor, "camera_indices 3 4"]:
        """Indexing into camera adjustments.
        Args:
            indices: indices of Cameras to optimize.
        Returns:
            Transformation matrices from optimized camera coordinates
            to given camera coordinates.
        """
        outputs = []

        # Apply learned transformation delta.
        if self.config.mode == "off":
            pass
        elif self.config.mode == "SO3xR3":
            outputs.append(exp_map_SO3xR3(self.pose_adjustment[indices, :]))
        elif self.config.mode == "SE3":
            outputs.append(exp_map_SE3(self.pose_adjustment[indices, :]))
        elif self.config.mode == "shared_SO3xR3":
            outputs.append(exp_map_SO3xR3(self.pose_adjustment).tile((indices.shape[0], 1, 1)))
        else:
            assert_never(self.config.mode)

        # Detach non-trainable indices by setting to identity transform
        if self.non_trainable_camera_indices is not None:
            if self.non_trainable_camera_indices.device != self.pose_adjustment.device:
                self.non_trainable_camera_indices = self.non_trainable_camera_indices.to(self.pose_adjustment.device)
            # outputs[0][self.non_trainable_camera_indices] = torch.eye(4, device=self.pose_adjustment.device)[:3, :4]
            if self.non_trainable_camera_filter.device != self.pose_adjustment.device:
                self.non_trainable_camera_filter = self.non_trainable_camera_filter.to(self.pose_adjustment.device)
            non_trainable_camera_filter = self.non_trainable_camera_filter[indices]
            outputs[0][non_trainable_camera_filter] = torch.eye(4, device=self.pose_adjustment.device)[:3, :4]

        # Return: identity if no transforms are needed, otherwise multiply transforms together.
        if len(outputs) == 0:
            # Note that using repeat() instead of tile() here would result in unnecessary copies.
            return torch.eye(4, device=self.device)[None, :3, :4].tile(indices.shape[0], 1, 1)
        return functools.reduce(pose_utils.multiply, outputs)

    def apply_to_raybundle(self, raybundle: RayBundle) -> None:
        """Apply the pose correction to the raybundle"""
        if self.config.mode != "off":
            correction_matrices = self(raybundle.camera_indices.squeeze())  # type: ignore
            raybundle.origins = raybundle.origins + correction_matrices[:, :3, 3]
            raybundle.directions = torch.bmm(correction_matrices[:, :3, :3], raybundle.directions[..., None]).squeeze()

    def apply_to_camera(self, camera: Cameras) -> None:
        """Apply the pose correction to the raybundle"""
        if self.config.mode != "off":
            assert camera.metadata is not None, "Must provide id of camera in its metadata"
            assert "cam_idx" in camera.metadata, "Must provide id of camera in its metadata"
            camera_idx = camera.metadata["cam_idx"]
            adj = self(torch.tensor([camera_idx], dtype=torch.long, device=camera.device))  # type: ignore
            adj = torch.cat([adj, torch.Tensor([0, 0, 0, 1])[None, None].to(adj)], dim=1)
            camera.camera_to_worlds = torch.bmm(camera.camera_to_worlds, adj)

    def get_loss_dict(self, loss_dict: dict) -> None:
        """Add regularization"""
        if self.config.mode != "off":
            loss_dict[f"camera_opt_regularizer{self.suffix}"] = (
                self.pose_adjustment[:, :3].norm(dim=-1).mean() * self.config.trans_l2_penalty
                + self.pose_adjustment[:, 3:].norm(dim=-1).mean() * self.config.rot_l2_penalty
            ) * self.config.penalty_scale

    def get_correction_matrices(self):
        """Get optimized pose correction matrices"""
        return self(torch.arange(0, self.num_cameras).long())

    def get_metrics_dict(self, metrics_dict: dict) -> None:
        """Get camera optimizer metrics"""
        if self.config.mode != "off":
            metrics_dict[f"camera_opt_translation{self.suffix}"] = self.pose_adjustment[:, :3].norm()
            metrics_dict[f"camera_opt_rotation{self.suffix}"] = self.pose_adjustment[:, 3:].norm()

    def get_param_groups(self, param_groups: dict, name: str = "camera_opt") -> None:
        """Get camera optimizer parameters"""
        camera_opt_params = list(self.parameters())
        if self.config.mode != "off":
            assert len(camera_opt_params) > 0
            param_groups[name] = camera_opt_params
        else:
            assert len(camera_opt_params) == 0
