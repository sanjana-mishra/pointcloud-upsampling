import os
import random
import numpy as np

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
import torch.nn.functional as F

from pointnet import PointNetEncoder, TNet

from utils_samplers import *


class PointNetSetAbstraction(nn.Module):
    def __init__(self, npoint, radius, nsample, in_channel, mlp, group_all):
        super(PointNetSetAbstraction, self).__init__()
        self.npoint = npoint
        self.radius = radius
        self.nsample = nsample
        self.mlp_convs = nn.ModuleList()
        self.mlp_bns = nn.ModuleList()
        last_channel = in_channel
        for out_channel in mlp:
            self.mlp_convs.append(nn.Conv2d(last_channel, out_channel, 1))
            self.mlp_bns.append(nn.BatchNorm2d(out_channel))
            last_channel = out_channel
        self.group_all = group_all

    def forward(self, xyz, points):
        """
        Input:
            xyz: input points position data, [B, C, N]
            points: input points data, [B, D, N]
        Return:
            new_xyz: sampled points position data, [B, C, S]
            new_points: sample points feature data, [B, D', S]
        """
        xyz = xyz.permute(0, 2, 1)
        if points is not None:
            points = points.permute(0, 2, 1)

        if self.group_all:
            new_xyz, new_points = sample_and_group_all(xyz, points)
        else:
            new_xyz, new_points = sample_and_group(
                self.npoint, self.radius, self.nsample, xyz, points)
        # The sample and group functions take in a [B, C, N] and [B, D (where D = C+X), N] tensors and return
        # [B, N, C], [B, N, S, D+3] tensors since new_points gets concated in the front with normalized x,y,z
        # for example a points tensor with [B, 9, N] as input will be returned as [B, 12, S, N] as output

        # new_xyz: sampled points position data, [B, npoint, C]
        # new_points: sampled points data, [B, npoint, nsample, C+D]
        new_points = new_points.permute(
            0, 3, 2, 1)  # [B, C+D, nsample, npoint]
        for i, conv in enumerate(self.mlp_convs):
            bn = self.mlp_bns[i]
            new_points = F.relu(bn(conv(new_points)))

        new_points = torch.max(new_points, 2)[0]
        new_xyz = new_xyz.permute(0, 2, 1)
        return new_xyz, new_points


class PointNetFeaturePropagation(nn.Module):
    def __init__(self, in_channel, mlp):
        super(PointNetFeaturePropagation, self).__init__()
        self.mlp_convs = nn.ModuleList()
        self.mlp_bns = nn.ModuleList()
        last_channel = in_channel
        for out_channel in mlp:
            self.mlp_convs.append(nn.Conv1d(last_channel, out_channel, 1))
            self.mlp_bns.append(nn.BatchNorm1d(out_channel))
            last_channel = out_channel

    def forward(self, xyz1, xyz2, points1, points2):
        """
        Input:
            xyz1: input points position data, [B, C, N] (eg: [B, 3, 1024]) 
            xyz2: sampled input points position data, [B, C, S] (eg: [B, 3, 512])
            points1: input points data, [B, D, N] (eg: None)
            points2: input points data, [B, D, S] (eg: [B, 128, 512])
        Return:
            new_points: upsampled points data, [B, D', N] (eg: [B, 64, 1024])
        """
        xyz1 = xyz1.permute(0, 2, 1)  # [B, N, C]
        xyz2 = xyz2.permute(0, 2, 1)  # [B, S, C]

        points2 = points2.permute(0, 2, 1)  # [B, S, D]
        B, N, C = xyz1.shape
        _, S, _ = xyz2.shape

        if S == 1:
            interpolated_points = points2.repeat(1, N, 1)
        else:
            # [B, 1024, 512] = [B, 1024, 3], [B, 512, 3]
            dists = square_distance(xyz1, xyz2)
            # sort distances with scending order of distance [B, 1024, 512]
            dists, idx = dists.sort(dim=-1)
            # first 3 nearest neighbors [B, N=1024, M=3]
            dists, idx = dists[:, :, :3], idx[:, :, :3]

            dist_recip = 1.0 / (dists + 1e-8)
            norm = torch.sum(dist_recip, dim=2, keepdim=True)
            weight = dist_recip / norm
            interpolated_points = torch.sum(index_points(
                points2, idx) * weight.view(B, N, 3, 1), dim=2)

        if points1 is not None:
            points1 = points1.permute(0, 2, 1)
            new_points = torch.cat([points1, interpolated_points], dim=-1)
        else:
            new_points = interpolated_points

        new_points = new_points.permute(0, 2, 1)
        for i, conv in enumerate(self.mlp_convs):
            bn = self.mlp_bns[i]
            new_points = F.relu(bn(conv(new_points)))
        return new_points

# PointNet++ encoder which extracts multiscale features from input point cloud.


class PointNetPPEncoder(nn.Module):
    def __init__(self, npoint=1024, nlayers=4, radii=[0.05, 0.1, 0.2, 0.3], nsamples=[32, 32, 32, 32], is_color=True, is_normal=False):
        super(PointNetPPEncoder, self).__init__()
        assert len(radii) == nlayers == len(nsamples)
        # Channels accounting for color and or normals for each point
        self.nlayers = nlayers
        self.additional_channels = 0
        if is_color:
            self.additional_channels += 3
        if is_normal:
            self.additional_channels += 3
        self.is_color = is_color
        self.is_normal = is_normal
        self.npoint = npoint
        self.npoints = [
            npoint,
            npoint // 2,
            npoint // 4,
            npoint // 8
        ]
        self.radii = radii
        self.nsamples = nsamples
        self.mlplists = [
            [32, 32, 64],
            [64, 64, 128],  # 0
            [128, 128, 256],  # 1
            [256, 256, 512]  # 2
        ]
        # Feature Extraction at Multiple Scales inside each set abstraction layer to get multi scale features
        self.salayers = nn.ModuleList()

        # C + 3 is done in the input channels because sample_and_group function concats normalized x,y,z
        self.lastinput = 3 + self.additional_channels
        for i in range(nlayers):
            self.salayers.append(PointNetSetAbstraction(
                self.npoints[i], self.radii[i], self.nsamples[i], self.lastinput + 3, self.mlplists[i], False))
            self.lastinput = self.mlplists[i][-1]

    def forward(self, xyz):
        B, C, N = xyz.size()
        # split the xyz coords from the color and normals
        l_xyz = [None for i in range(self.nlayers+1)]
        l_points = [None for i in range(self.nlayers+1)]
        if self.is_normal or self.is_color:
            l_points[0] = xyz
            l_xyz[0] = xyz[:, :3, :]

        # Now send the xyz and points through set abstraction layers
        print(f"l_xyz: {l_xyz[0].size()}, l_points: {l_points[0].size()}")
        for i in range(self.nlayers):
            l_xyz[i+1], l_points[i+1] = self.salayers[i](l_xyz[i], l_points[i])
            print(
                f"l_xyz: {l_xyz[i+1].size()}, l_points: {l_points[i+1].size()}")

        # Now we have multi scale features at every layer of point cloud abstraction
        # We can basically do anything with these learned features
        return l_xyz, l_points


if __name__ == '__main__':
    import torch
    model = PointNetPPEncoder(is_color=True, is_normal=True)
    xyz = torch.rand(6, 9, 1024)
    (model(xyz))
