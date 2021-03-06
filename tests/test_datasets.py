import os
import torch
import torchvision.transforms as transforms
import pytest
import pytorch_lightning as pl
import shutil
import imgaug.augmenters as iaa


def test_heatmap_dataset():
    from pose_est_nets.datasets.datasets import BaseTrackingDataset, HeatmapDataset

    data_transform = []
    data_transform.append(
        iaa.Resize({"height": 384, "width": 384})
    )  # dlc dimensions need to be repeatably divisable by 2
    imgaug_transform = iaa.Sequential(data_transform)

    regData = BaseTrackingDataset(
        root_directory="toy_datasets/toymouseRunningData",
        csv_path="CollectedData_.csv",
        header_rows=[1, 2],
        imgaug_transform=imgaug_transform,
    )
    heatmapData = HeatmapDataset(
        root_directory="toy_datasets/toymouseRunningData",
        csv_path="CollectedData_.csv",
        header_rows=[1, 2],
        imgaug_transform=imgaug_transform,
    )
    # first test: both datasets provide the same image at index 0
    assert torch.equal(regData[0][0], heatmapData[0][0])

    # we get the desired image height and width
    assert heatmapData[0][0].shape[1:] == (
        heatmapData.height,
        heatmapData.width,
    )
    image, heatmaps, keypoints = heatmapData[0]
    assert image.shape == (3, 384, 384)  # resized image shape
    assert keypoints.shape == (34,)
    assert heatmaps.shape[1:] == heatmapData.output_shape
    assert type(regData[0][1]) == torch.Tensor
    numKeypoints = regData.keypoints.shape[1]

    # for idx in range(numLabels):
    #     if torch.any(torch.isnan(regData.__getitem__(0)[1][idx])):
    #         print("there is any nan here")
    #         print(torch.max(heatmapData.__getitem__(0)[1][idx]))
    #         assert torch.max(heatmapData.__getitem__(0)[1][idx]) == torch.tensor(0)
    #     else:  # TODO: the below isn't passing on idx 5, we have an all zeros heatmap for a label vec without nans
    #         print(f"idx {idx}")
    #         print("no nan's here")
    #         print("labels: {}")
    #         print(torch.unique(heatmapData.__getitem__(0)[1][idx]))
    #         print("item {}".format(torch.max(heatmapData.__getitem__(0)[1][idx])))
    #         assert torch.max(heatmapData.__getitem__(0)[1][idx]) != torch.tensor(0)

    # print(heatmapData.__getitem__(11)[1])

    # remove model/data from gpu; then cache can be cleared
    del regData
    del heatmapData
    del image
    del heatmaps
    del keypoints
    torch.cuda.empty_cache()  # remove tensors from gpu


test_heatmap_dataset()
