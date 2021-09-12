from pose_est_nets.models.base_resnet import BaseFeatureExtractor
import torch
import torchvision.models as models
from torch.nn import functional as F
from torch import nn
from pytorch_lightning.core.lightning import LightningModule
from torch.optim import Adam
from torch.optim.lr_scheduler import ReduceLROnPlateau
from typing import Any, Callable, Optional, Tuple, List
from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked
import numpy as np
from pose_est_nets.utils.heatmap_tracker_utils import (
    find_subpixel_maxima,
    largest_factor,
    format_mouse_data,
)

patch_typeguard()  # use before @typechecked


class HeatmapTracker(BaseFeatureExtractor):
    def __init__(
        self,
        num_targets: int,
        resnet_version: Optional[int] = 18,
        downsample_factor: Optional[
            int
        ] = 2,  # TODO: downsample_factor may be in mismatch between datamodule and model
        pretrained: Optional[bool] = False,
        last_resnet_layer_to_get: Optional[int] = -3,
    ) -> None:
        """
        TODO: edit this. note, last_resnet_layer_to_get is different from the regression net, on purpose.
        Initializes a DLC-like model with resnet backbone inherited from BaseFeatureExtractor
        :param num_targets: number of body parts times 2 (x,y) coords
        :param resnet_version: The ResNet variant to be used (e.g. 18, 34, 50, 101, or 152). Essentially specifies how
            large the resnet will be.
        :param transfer:  Flag to indicate whether this is a transfer learning task or not; defaults to false,
            meaning the entire model will be trained unless this flag is provided
        """
        super().__init__(  # execute BaseFeatureExtractor.__init__()
            resnet_version=resnet_version,
            pretrained=pretrained,
            last_resnet_layer_to_get=last_resnet_layer_to_get,
        )
        self.__dict__.update(locals())  # TODO: what is this?

        self.num_filters_for_upsampling = self.backbone.fc.in_features
        self.num_targets = num_targets
        self.num_keypoints = num_targets // 2
        self.downsample_factor = downsample_factor
        self.coordinate_scale = torch.tensor(2 ** downsample_factor, device=self.device)
        self.upsampling_layers = self.make_upsampling_layers()
        self.initialize_upsampling_layers()
        # I'm up to this line. the below is older
        self.upsampling_layers = []
        # TODO: Add normalization
        # TODO: Should depend on input size?
        # TODO: all of the following can be properties?
        if downsample_factor == 3:
            self.upsampling_layers += [  # shape = [batch, 2048, 12, 12] for resnet 50 and above, assuming it is chopped before the last pulling
                # nn.Upsample(scale_factor = 2, mode = 'bilinear'),
                nn.PixelShuffle(2),
                nn.ConvTranspose2d(
                    in_channels=int(self.num_filters_for_upsampling / 4),
                    out_channels=self.num_keypoints,
                    kernel_size=(3, 3),
                    stride=(2, 2),
                    padding=(1, 1),
                    output_padding=(1, 1),
                ),  # [batch, 17, 48, 48]
            ]
            self.upsampling_layers = nn.Sequential(*self.upsampling_layers)
            torch.nn.init.xavier_uniform_(self.upsampling_layers[-1].weight)
            torch.nn.init.zeros_(self.upsampling_layers[-1].bias)
        elif downsample_factor == 2:
            self.upsampling_layers += [  # shape = [batch, 2048, 12, 12]
                nn.PixelShuffle(2),
                nn.ConvTranspose2d(
                    in_channels=int(self.num_filters_for_upsampling / 4),
                    out_channels=self.num_keypoints,
                    kernel_size=(3, 3),
                    stride=(2, 2),
                    padding=(1, 1),
                    output_padding=(1, 1),
                ),  # [batch, 17, 48, 48]
                nn.ConvTranspose2d(
                    in_channels=self.num_keypoints,
                    out_channels=self.num_keypoints,
                    kernel_size=(3, 3),
                    stride=(2, 2),
                    padding=(1, 1),
                    output_padding=(1, 1),
                ),  # [batch, 17, 96, 96]
            ]
            self.upsampling_layers = nn.Sequential(*self.upsampling_layers)
            torch.nn.init.xavier_uniform_(self.upsampling_layers[-1].weight)
            torch.nn.init.zeros_(self.upsampling_layers[-1].bias)
            torch.nn.init.xavier_uniform_(self.upsampling_layers[-2].weight)
            torch.nn.init.zeros_(self.upsampling_layers[-2].bias)
        else:
            print("downsample factor not supported!")
            exit()

        # self.batch_size = 16 #for autoscale batchsize
        # self.num_workers = 0

    def initialize_upsampling_layers(self):
        # TODO: test that running this method changes the weights and biases
        """loop over the Conv2DTranspose layers and initialize them"""
        for index, layer in enumerate(self.upsampling_layers):
            if index > 0:  # we ignore the PixelShuffle
                torch.nn.init.xavier_uniform_(layer.weight)
                torch.nn.init.zeros_(layer.bias)

    @typechecked
    def make_upsampling_layers(self) -> list:
        upsampling_layers = [nn.PixelShuffle(2)]
        upsampling_layers.append(
            nn.ConvTranspose2d(
                in_channels=self.num_filters_for_upsampling // 4,
                out_channels=self.num_keypoints,
                kernel_size=(3, 3),
                stride=(2, 2),
                padding=(1, 1),
                output_padding=(1, 1),
            )
        )

        if self.downsample_factor == 2:  # make the heatmaps bigger
            upsampling_layers.append(
                nn.ConvTranspose2d(
                    in_channels=self.num_keypoints,
                    out_channels=self.num_keypoints,
                    kernel_size=(3, 3),
                    stride=(2, 2),
                    padding=(1, 1),
                    output_padding=(1, 1),
                )
            )

        return upsampling_layers

    @typechecked
    def forward(
        self, x: TensorType["Batch_Size", 3, "Height", "Width"]
    ) -> TensorType[
        "Batch_Size", "Num_Keypoints", "Out_Height", "Out_Width"
    ]:  # how do I use a variable to indicate number of keypoints
        """
        Forward pass through the network
        :param x: input
        :return: output of network
        """
        # self.feature_extractor.eval()
        # with torch.no_grad():
        representations = self.feature_extractor(x)
        # TODO: move to tests
        # TODO: [12,12] is independent of resnet architecture, but not sure if independent of image size. potentially different for non square images?
        assert (
            torch.tensor(representations.shape[-2:]) == torch.tensor([12, 12])
        ).all()

        out = self.upsampling_layers(representations)
        assert (
            torch.tensor(out.shape[-2:])
            == torch.tensor(x.shape[-2:]) // (2 ** self.downsample_factor)
        ).all()
        return out

    @staticmethod
    @typechecked
    def heatmap_loss(
        y: TensorType["Batch_Size", "Num_Keypoints", "Out_Height", "Out_Width"],
        y_hat: TensorType["Batch_Size", "Num_Keypoints", "Out_Height", "Out_Width"],
    ) -> TensorType[()]:
        """
        Computes mse loss between ground truth (x,y) coordinates and predicted (x^,y^) coordinates
        :param y: ground truth. shape=(num_targets, 2)
        :param y_hat: prediction. shape=(num_targets, 2)
        :return: mse loss
        """
        # apply mask, only computes loss on heatmaps where the ground truth heatmap is not all zeros (i.e., not an occluded keypoint)
        max_vals = torch.amax(y, dim=(2, 3))
        zeros = torch.zeros(size=(y.shape[0], y.shape[1]), device=y_hat.device)
        non_zeros = ~torch.eq(max_vals, zeros)
        mask = torch.reshape(non_zeros, [non_zeros.shape[0], non_zeros.shape[1], 1, 1])
        # compute loss
        loss = F.mse_loss(
            torch.masked_select(y_hat, mask), torch.masked_select(y, mask)
        )
        return loss

    @typechecked
    # what are we doing about NANS?
    def pca_2view_loss(
        self,
        y_hat: TensorType["Batch_Size", "Num_Keypoints", "Out_Height", "Out_Width"],
    ) -> TensorType[()]:
        # TODO: add conditions regarding epsilon?
        kernel_size = np.min(self.output_shape)  # change from numpy to torch
        kernel_size = (kernel_size // largest_factor(kernel_size)) + 1
        keypoints = find_subpixel_maxima(
            y_hat.detach(),  # TODO: why detach? could keep everything on GPU?
            torch.tensor(kernel_size, device=self.device),
            torch.tensor(self.output_sigma, device=self.device),
            self.upsample_factor,  # TODO: these are coming from self, shouldn't be inputs?
            self.coordinate_scale,
            self.confidence_scale,
        )
        keypoints = keypoints[:, :, :2]
        data_arr = format_mouse_data(keypoints)
        abs_proj_discarded = torch.abs(
            torch.matmul(data_arr.T, self.pca_param_dict["discarded_eigenvectors"].T)
        )
        epsilon_masked_proj = abs_proj_discarded.masked_fill(
            mask=abs_proj_discarded > self.pca_param_dict["epsilon"], value=0.0
        )
        assert (epsilon_masked_proj >= 0.0).all()  # every element should be positive
        assert torch.mean(epsilon_masked_proj) <= torch.mean(
            abs_proj_discarded
        )  # the scalar loss should be smaller after zeroing out elements.
        return torch.mean(epsilon_masked_proj)

    def training_step(self, data, batch_idx):
        # x, y_heatmap, y_keypoints = data
        x, y = data
        # forward pass
        y_hat = self.forward(x)
        # compute loss
        heatmap_loss = self.heatmap_loss(y, y_hat)
        # heatmap_loss = self.heatmap_loss(y_heatmap, y_hat)
        pca_view_loss = self.pca_2view_loss(y_hat)
        loss = heatmap_loss + pca_view_loss

        # ppca_loss =
        # log training loss
        self.log(
            "train_loss", loss, on_step=True, on_epoch=True, prog_bar=True, logger=True
        )
        self.log(
            "pca_loss",
            pca_view_loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )

        self.log(
            "heatmap_loss",
            heatmap_loss,
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return {"loss": loss}

    def validation_step(self, data: Tuple, batch_idx: int) -> None:
        x, y = data
        y_hat = self.forward(x)
        # compute loss
        loss = self.heatmap_loss(y, y_hat)
        # log validation loss
        self.log("val_loss", loss, prog_bar=True, logger=True)

    def test_step(self, data, batch_idx):
        self.validation_step(data, batch_idx)

    def computeSubPixMax(self, heatmaps_pred, heatmaps_y, threshold):
        assert hasattr(self, "output_shape")
        kernel_size = np.min(self.output_shape)
        kernel_size = (kernel_size // largest_factor(kernel_size)) + 1
        pred_keypoints = find_subpixel_maxima(
            heatmaps_pred.detach(),
            torch.tensor(kernel_size, device=heatmaps_pred.device),
            torch.tensor(self.output_sigma, device=heatmaps_pred.device),
            self.upsample_factor,
            self.coordinate_scale,
            self.confidence_scale,
        )
        y_keypoints = find_subpixel_maxima(
            heatmaps_y.detach(),
            torch.tensor(kernel_size, device=heatmaps_pred.device),
            torch.tensor(self.output_sigma, device=heatmaps_pred.device),
            self.upsample_factor,
            self.coordinate_scale,
            self.confidence_scale,
        )
        pred_keypoints = pred_keypoints[0]
        y_keypoints = y_keypoints[0]
        if threshold:
            # for i in range(pred_keypoints.shape[0]): # pred_keypoints is shape(num_keypoints, 3) the last entry being (x,y, confidence)
            #     if pred_keypoints[i, 2] > 0.008: #threshold for low confidence predictions
            #         pred_kpts_list.append(pred_keypoints[i, :2].cpu().numpy())
            #     if y_keypoints[i, 2] > 0.008:
            #         y_kpts_list.append(y_keypoints[i, :2].cpu().numpy())
            # print(pred_kpts_list, y_kpts_list)
            num_threshold = torch.tensor(0.001, device=heatmaps_pred.device)
            pred_mask = torch.gt(pred_keypoints[:, 2], num_threshold)
            pred_mask = pred_mask.unsqueeze(-1)
            y_mask = torch.gt(y_keypoints[:, 2], num_threshold)
            y_mask = y_mask.unsqueeze(-1)
            pred_keypoints = torch.masked_select(pred_keypoints, pred_mask).reshape(
                -1, 3
            )
            y_keypoints = torch.masked_select(y_keypoints, y_mask).reshape(-1, 3)

        pred_keypoints = pred_keypoints[:, :2]  # getting rid of the actual max value
        y_keypoints = y_keypoints[:, :2]
        return pred_keypoints, y_keypoints

    def configure_optimizers(self):
        optimizer = Adam(self.parameters(), lr=1e-3)
        scheduler = ReduceLROnPlateau(optimizer, factor=0.2, patience=20, verbose=True)
        return {
            "optimizer": optimizer,
            "lr_scheduler": scheduler,
            "monitor": "val_loss",
        }