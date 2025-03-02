import os
import cv2
from PIL import Image
import numpy as np
import pyvista as pv
import torch
import torch.nn as nn
from scipy.ndimage.morphology import binary_erosion
from Reconstruction_3D.models.depth_pred_with_sph_inpaint import Net as Depth_inpaint_net
from Reconstruction_3D.models.depth_pred_with_sph_inpaint import Model as DepthInpaintModel
from Reconstruction_3D.networks.networks import Unet_3D
from Reconstruction_3D.toolbox.cam_bp.cam_bp.modules.camera_backprojection_module import Camera_back_projection_layer
from Reconstruction_3D.toolbox.cam_bp.cam_bp.functions import SphericalBackProjection
from Reconstruction_3D.toolbox.spherical_proj import gen_sph_grid
from os import makedirs
from os.path import join
from Reconstruction_3D.util import util_img
from Reconstruction_3D.util import util_sph
import torch.nn.functional as F
from Reconstruction_3D.toolbox.spherical_proj import sph_pad
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D


class Model(DepthInpaintModel):
    @classmethod
    def add_arguments(cls, parser):
        parser, unique_params = DepthInpaintModel.add_arguments(parser)
        parser.add_argument('--inpaint_path', default=None, type=str,
                            help="path to pretrained inpainting module")
        parser.add_argument('--surface_weight', default=1.0, type=float,
                            help="weight for voxel surface prediction")
        unique_params_model = {'surface_weight', 'joint_train', 'inpaint_path'}
        return parser, unique_params.union(unique_params_model)

    def __init__(self, opt, logger):
        super(Model, self).__init__(opt, logger)
        self.joint_train = opt.joint_train
        if self.joint_train:
            self.requires.append('voxel')
        else:
            self.requires = ['rgb', 'silhou', 'voxel']
        self.gt_names.append('voxel')
        self._metrics += ['voxel_loss', 'surface_loss']
        self.net = Net(opt, Model)
        self.optimizer = self.adam(
            self.net.parameters(),
            lr=opt.lr,
            **self.optim_params
        )
        self._nets = [self.net]
        self._optimizers = [self.optimizer]
        self.init_vars(add_path=True)
        if not self.joint_train:
            self.init_weight(self.net.refine_net)

    def __str__(self):
        string = "Full model of GenRe."
        if self.joint_train:
            string += ' Jointly training all the modules.'
        else:
            string += ' Only training the refinement module'
        return string

    def compute_loss(self, pred):
        loss = 0
        loss_data = {}
        if self.joint_train:
            loss, loss_data = super(Model, self).compute_loss(pred)
        voxel_loss = F.binary_cross_entropy_with_logits(pred['pred_voxel'], self._gt.voxel)
        sigmoid_voxel = torch.sigmoid(pred['pred_voxel'])
        surface_loss = F.binary_cross_entropy(sigmoid_voxel * self._gt.voxel, self._gt.voxel)
        loss += voxel_loss.mean()
        loss += surface_loss.mean() * self.opt.surface_weight
        loss_data['voxel_loss'] = voxel_loss.mean().item()
        loss_data['surface_loss'] = surface_loss.mean().item() * self.opt.surface_weight
        loss_data['loss'] = loss.mean().item()
        return loss, loss_data

    def pack_output(self, pred, batch, add_gt=True):
        pack = {}
        if self.joint_train:
            pack = super(Model, self).pack_output(pred, batch, add_gt=add_gt)
        pack['pred_voxel'] = pred['pred_voxel'].cpu().numpy()
        pack['pred_proj_sph_partial'] = pred['pred_voxel'].cpu().numpy()
        pack['pred_proj_depth'] = pred['pred_proj_depth'].cpu().numpy()
        pack['pred_proj_sph_full'] = pred['pred_proj_sph_full'].cpu().numpy()
        if add_gt:
            pack['gt_voxel'] = batch['voxel'].numpy()
        return pack

    @classmethod
    def preprocess(cls, data, mode='train'):
        dataout = DepthInpaintModel.preprocess(data, mode)
        if 'voxel' in dataout:
            val = dataout['voxel'][0, :, :, :]
            val = np.transpose(val, (0, 2, 1))
            val = np.flip(val, 2)
            voxel_surface = val - binary_erosion(val, structure=np.ones((3, 3, 3)), iterations=2).astype(float)
            voxel_surface = voxel_surface[None, ...]
            voxel_surface = np.clip(voxel_surface, 0, 1)
            dataout['voxel'] = voxel_surface
        return dataout


class Net(nn.Module):
    def __init__(self, opt, base_class):
        super().__init__()
        self.base_class = base_class
        self.depth_and_inpaint = Depth_inpaint_net(opt, base_class)
        self.refine_net = Unet_3D()
        self.proj_depth = Camera_back_projection_layer()
        self.joint_train = opt.joint_train
        self.register_buffer('grid', gen_sph_grid())
        self.grid = self.grid.expand(1, -1, -1, -1, -1)
        self.proj_spherical = SphericalBackProjection().apply
        self.margin = opt.padding_margin
        if opt.inpaint_path is not None:
            state_dicts = torch.load(opt.inpaint_path)
            self.depth_and_inpaint.load_state_dict(state_dicts['nets'][0])

    def forward(self, input_struct):
        if not self.joint_train:
            with torch.no_grad():
                out_1 = self.depth_and_inpaint(input_struct)
        else:
            out_1 = self.depth_and_inpaint(input_struct)
        # use proj_depth and sph_in
        proj_depth = out_1['proj_depth']
        pred_sph = out_1['pred_sph_full']
        pred_proj_sph = self.backproject_spherical(pred_sph)

        # pred_proj_sph_copy = pred_proj_sph
        # pred_proj_sph_cpu = pred_proj_sph_copy.cpu()
        # pred_proj_sph_numpy = pred_proj_sph_cpu.squeeze().numpy()
        # np.save('/home/oem/Downloads/code/output/pred_proj_sph.npy', pred_proj_sph_numpy)
        # print(f'type of pred_proj_sph_numpy: {type(pred_proj_sph_numpy)}')

        proj_depth = torch.clamp(proj_depth / 50, 1e-5, 1 - 1e-5)
        refine_input = torch.cat((pred_proj_sph, proj_depth), dim=1)
        pred_voxel = self.refine_net(refine_input)
        out_1['pred_proj_depth'] = proj_depth
        out_1['pred_voxel'] = pred_voxel
        out_1['pred_proj_sph_full'] = pred_proj_sph
        return out_1

    def backproject_spherical(self, sph):
        batch_size, _, h, w = sph.shape
        grid = self.grid[0, :, :, :, :]
        grid = grid.expand(batch_size, -1, -1, -1, -1)
        crop_sph = sph[:, :, self.margin:h - self.margin, self.margin:w - self.margin]
        proj_df, cnt = self.proj_spherical(1 - crop_sph, grid, 128)
        mask = torch.clamp(cnt.detach(), 0, 1)
        proj_df = (-proj_df + 1 / 128) * 128
        proj_df = proj_df * mask
        return proj_df


class Model_test(Model):
    def __init__(self, opt, logger):
        super().__init__(opt, logger)
        self.requires = ['rgb', 'mask']  # mask for bbox cropping only
        self.input_names = ['rgb']
        self.init_vars(add_path=True)
        self.load_state_dict(opt.net_file, load_optimizer='auto')
        self.output_dir = opt.output_dir
        self.input_names.append('silhou')

    def __str__(self):
        return "Testing GenRe"

    @classmethod
    def preprocess_wrapper(cls, in_dict):
        silhou_thres = 0.95
        in_size = 480
        pad = 85
        im = in_dict['rgb']
        mask = in_dict['silhou']
        bbox = util_img.get_bbox(mask, th=silhou_thres)
        im_crop = util_img.crop(im, bbox, in_size, pad, pad_zero=False)
        silhou_crop = util_img.crop(in_dict['silhou'], bbox, in_size, pad, pad_zero=False)
        in_dict['rgb'] = im_crop
        in_dict['silhou'] = silhou_crop
        # Now the image is just like those we rendered
        out_dict = cls.preprocess(in_dict, mode='test')
        return out_dict

    def test_on_batch(self, batch_i, batch, use_trimesh=True):
        outdir = join(self.output_dir, 'batch%04d' % batch_i)
        makedirs(outdir, exist_ok=True)
        if not use_trimesh:
            pred = self.predict(batch, load_gt=False, no_grad=True)
        else:
            assert self.opt.batch_size == 1
            pred = self.forward_with_trimesh(batch)

        # print(f'type of pred: {type(pred)}')
        # print(f'keys of pred: {pred.keys()}')
        # TypeNormal = type(pred['normal'])
        # TypeDepth = type(pred['depth'])
        # TypeSilhou = type(pred['silhou'])
        # TypeDepthMinMax = type(pred['depth_minmax'])
        # TypeSphFull = type(pred['pred_sph_full'])
        # TypeSphPartial = type(pred['pred_sph_partial'])
        # TypeProjDepth = type(pred['pred_proj_depth'])
        # TypeProjSphFull = type(pred['pred_proj_sph_full'])
        # print(f'type of normal" {TypeNormal}')
        # print(f'type of depth" {TypeDepth}')
        # print(f'type of silhou" {TypeSilhou}')
        # print(f'type of depth minmax" {TypeDepthMinMax}')
        # print(f'type of sph full" {TypeSphFull}')
        # print(f'type of sph partial" {TypeSphPartial}')
        # print(f'type of proj depth" {TypeProjDepth}')
        # print(f'type of proj sph full" {TypeProjSphFull}')

        output = self.pack_output(pred, batch, add_gt=False)
        self.visualizer.visualize(output, batch_i, outdir)
        np.savez(outdir + '.npz', **output)

        Depth = pred['depth']
        # print(f'shape of depth: {Depth.shape}')
        Depth_image = Depth.cpu().float().numpy()
        Depth_image = Depth_image[0]
        Depth_image = np.transpose(Depth_image, (1, 2, 0))
        Depth_image = cv2.resize(Depth_image, (self.opt.image_size, self.opt.image_size))
        silhouette = Image.open(self.opt.input_mask)
        silhouette_numpy = np.array(silhouette)
        Depth_image[silhouette_numpy == 0] = 255
        Depth_image_path = os.path.join(self.opt.output_dir, 'batch0000', 'depth_image.png')
        cv2.imwrite(Depth_image_path, Depth_image)
        # plt.imshow(Depth_image)
        # plt.axis('off')
        # plt.savefig(Depth_image_path, bbox_inches='tight', pad_inches=0)
        # plt.show()

        pred_sph_partial = pred['pred_sph_partial']
        # print(f'shape of sph partial {pred_sph_partial.shape}')
        image_pred_sph_partial = pred_sph_partial.cpu()
        image_pred_sph_partial = image_pred_sph_partial.squeeze().numpy()
        sph_partial_path = os.path.join(self.opt.output_dir, 'batch0000', 'sph_partial.png')
        plt.imshow(image_pred_sph_partial, cmap='gray')
        plt.axis('off')
        plt.savefig(sph_partial_path, bbox_inches='tight', pad_inches=0)
        # plt.show()

        pred_sph_full = pred['pred_sph_full']
        # print(f'shape of sph full {pred_sph_full.shape}')
        image_pred_sph_full = pred_sph_full.cpu()
        image_pred_sph_full = image_pred_sph_full.squeeze().numpy()
        sph_full_path = os.path.join(self.opt.output_dir, 'batch0000', 'sph_full.png')
        plt.imshow(image_pred_sph_full, cmap='gray')
        plt.axis('off')
        plt.savefig(sph_full_path, bbox_inches='tight', pad_inches=0)
        # plt.show()

        Pred_proj_depth = pred['pred_proj_depth']
        # print(f'type of Pred_proj_depth: {type(Pred_proj_depth)}')
        # print(f'shape of Pred_proj_depth: {Pred_proj_depth.shape}')
        pred_proj_depth_cpu = Pred_proj_depth.squeeze().cpu()
        pred_proj_depth_vis = pred_proj_depth_cpu.numpy()
        proj_depth_path = os.path.join(self.opt.output_dir, 'batch0000', 'proj_depth.npy')
        np.save(proj_depth_path, pred_proj_depth_vis)
        # # 创建一个 PyVista 网格
        # grid = pv.UniformGrid()
        # grid.dimensions = pred_proj_depth_vis.shape
        # # grid.origin = (0, 0, 0)
        # # grid.spacing = (1, 1, 1)
        # # 将数据赋值给网格
        # grid.point_arrays['values'] = pred_proj_depth_vis.flatten(order='F')  # 'F' for Fortran order
        # # 创建一个 Plotter
        # plotter = pv.Plotter()
        # # 添加网格到 Plotter
        # plotter.add_mesh(grid, cmap='viridis')
        # # 显示可视化
        # plotter.show()

        # pred_proj_sph_full = pred['pred_proj_sph_full']
        # # print(f'shape of sph full {pred_proj_sph_full.shape}')
        # pred_proj_sph_full_cpu = pred_proj_sph_full.squeeze().cpu()
        # pred_proj_sph_full_vis = pred_proj_sph_full_cpu.numpy()
        # proj_depth_path = os.path.join(self.opt.output_dir, 'batch0000', 'proj_sph_full.npy')
        # np.save(proj_depth_path, pred_proj_sph_full_vis)

        # # 创建一个 PyVista 网格
        # grid = pv.UniformGrid()
        # grid.dimensions = pred_proj_sph_full_vis.shape
        # # grid.origin = (0, 0, 0)
        # # grid.spacing = (1, 1, 1)
        # # 将数据赋值给网格
        # grid.point_arrays['values'] = pred_proj_sph_full_vis.flatten(order='F')  # 'F' for Fortran order
        # # 创建一个 Plotter
        # plotter = pv.Plotter()
        # # 添加网格到 Plotter
        # plotter.add_mesh(grid, cmap='viridis')
        # # 显示可视化
        # plotter.show()


    def pack_output(self, pred, batch, add_gt=True):
        pack = {}
        pack['pred_voxel'] = pred['pred_voxel'].cpu().numpy()
        pack['rgb_path'] = batch['rgb_path']
        #pack['pred_proj_depth'] = pred['pred_proj_depth'].cpu().numpy()
        #pack['pred_proj_sph_full'] = pred['pred_proj_sph_full'].cpu().numpy()
        #pack['pred_sph_partial'] = pred['pred_sph_partial'].cpu().numpy()
        #pack['pred_depth'] = pred['pred_depth'].cpu().numpy()
        #pack['pred_depth_minmax'] = pred['depth_minmax'].cpu().numpy()
        #pack['pred__minmax'] = pred['depth_minmax'].cpu().numpy()
        if add_gt:
            pack['gt_voxel'] = batch['voxel'].numpy()
        return pack

    def forward_with_trimesh(self, batch):
        self.load_batch(batch, include_gt=False)
        with torch.no_grad():
            pred_1 = self.net.depth_and_inpaint.net1.forward(self._input)
        pred_abs_depth = self.net.depth_and_inpaint.get_abs_depth(pred_1, self._input)
        proj = self.net.depth_and_inpaint.proj_depth(pred_abs_depth)
        pred_depth = self.net.depth_and_inpaint.base_class.postprocess(pred_1['depth'].detach())
        silhou = self.net.base_class.postprocess(self._input.silhou).detach()
        pred_depth = pred_depth.cpu().numpy()
        pred_depth_minmax = pred_1['depth_minmax'].detach().cpu().numpy()[0, :]
        silhou = silhou.cpu().numpy()[0, 0, :, :]
        pack = {'depth': pred_depth, 'depth_minmax': pred_depth_minmax}
        rendered_sph = util_sph.render_spherical(pack, silhou)[None, None, ...]
        rendered_sph = torch.from_numpy(rendered_sph).float().cuda()
        rendered_sph = sph_pad(rendered_sph)
        with torch.no_grad():
            out2 = self.net.depth_and_inpaint.net2(rendered_sph)
        pred_proj_sph = self.net.backproject_spherical(out2['spherical'])
        pred_proj_sph = torch.transpose(pred_proj_sph, 3, 4)
        pred_proj_sph = torch.flip(pred_proj_sph, [3])

        pred_proj_sph_copy = pred_proj_sph
        pred_proj_sph_cpu = pred_proj_sph_copy.cpu()
        pred_proj_sph_numpy = pred_proj_sph_cpu.squeeze().numpy()
        pred_proj_sph_path = os.path.join(self.opt.output_dir, 'batch0000', 'pred_proj_sph.npy')
        np.save(pred_proj_sph_path, pred_proj_sph_numpy)
        # print(f'type of pred_proj_sph_numpy: {type(pred_proj_sph_numpy)}')

        proj = torch.transpose(proj, 3, 4)
        proj = torch.flip(proj, [3])

        proj_depth_copy = proj
        proj_depth_cpu = proj_depth_copy.cpu()
        proj_depth_numpy = proj_depth_cpu.squeeze().numpy()
        proj_depth_path = os.path.join(self.opt.output_dir, 'batch0000', 'proj_depth.npy')
        np.save(proj_depth_path, proj_depth_numpy)

        refine_input = torch.cat((pred_proj_sph, proj), dim=1)
        with torch.no_grad():
            pred_voxel = self.net.refine_net(refine_input)
        pred_1['pred_sph_full'] = out2['spherical']
        pred_1['pred_sph_partial'] = rendered_sph
        pred_1['pred_proj_depth'] = proj
        pred_1['pred_voxel'] = pred_voxel.flip([3]).transpose(3, 4)
        pred_1['pred_proj_sph_full'] = pred_proj_sph
        return pred_1
