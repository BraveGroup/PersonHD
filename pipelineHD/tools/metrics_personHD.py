import os
os.environ['CUDA_VISIBLE_DEVICES']='5'
import pathlib
import torch
import numpy as np
from imageio import imread
from scipy import linalg
from torch.nn.functional import adaptive_avg_pool2d
import glob
import argparse
from PIL import Image
import tqdm
import lpips
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models
import time
class InceptionV3(nn.Module):
    """Pretrained InceptionV3 network returning feature maps"""

    # Index of default block of inception to return,
    # corresponds to output of final average pooling
    DEFAULT_BLOCK_INDEX = 3

    # Maps feature dimensionality to their output blocks indices
    BLOCK_INDEX_BY_DIM = {
        64: 0,   # First max pooling features
        192: 1,  # Second max pooling featurs
        768: 2,  # Pre-aux classifier features
        2048: 3  # Final average pooling features
    }

    def __init__(self,
                 output_blocks=[DEFAULT_BLOCK_INDEX],
                 resize_input=True,
                 normalize_input=True,
                 requires_grad=False):
        """Build pretrained InceptionV3
        Parameters
        ----------
        output_blocks : list of int
            Indices of blocks to return features of. Possible values are:
                - 0: corresponds to output of first max pooling
                - 1: corresponds to output of second max pooling
                - 2: corresponds to output which is fed to aux classifier
                - 3: corresponds to output of final average pooling
        resize_input : bool
            If true, bilinearly resizes input to width and height 299 before
            feeding input to model. As the network without fully connected
            layers is fully convolutional, it should be able to handle inputs
            of arbitrary size, so resizing might not be strictly needed
        normalize_input : bool
            If true, normalizes the input to the statistics the pretrained
            Inception network expects
        requires_grad : bool
            If true, parameters of the model require gradient. Possibly useful
            for finetuning the network
        """
        super(InceptionV3, self).__init__()

        self.resize_input = resize_input
        self.normalize_input = normalize_input
        self.output_blocks = sorted(output_blocks)
        self.last_needed_block = max(output_blocks)

        assert self.last_needed_block <= 3, \
            'Last possible output block index is 3'

        self.blocks = nn.ModuleList()

        inception = models.inception_v3(pretrained=True)

        # Block 0: input to maxpool1
        block0 = [
            inception.Conv2d_1a_3x3,
            inception.Conv2d_2a_3x3,
            inception.Conv2d_2b_3x3,
            nn.MaxPool2d(kernel_size=3, stride=2)
        ]
        self.blocks.append(nn.Sequential(*block0))

        # Block 1: maxpool1 to maxpool2
        if self.last_needed_block >= 1:
            block1 = [
                inception.Conv2d_3b_1x1,
                inception.Conv2d_4a_3x3,
                nn.MaxPool2d(kernel_size=3, stride=2)
            ]
            self.blocks.append(nn.Sequential(*block1))

        # Block 2: maxpool2 to aux classifier
        if self.last_needed_block >= 2:
            block2 = [
                inception.Mixed_5b,
                inception.Mixed_5c,
                inception.Mixed_5d,
                inception.Mixed_6a,
                inception.Mixed_6b,
                inception.Mixed_6c,
                inception.Mixed_6d,
                inception.Mixed_6e,
            ]
            self.blocks.append(nn.Sequential(*block2))

        # Block 3: aux classifier to final avgpool
        if self.last_needed_block >= 3:
            block3 = [
                inception.Mixed_7a,
                inception.Mixed_7b,
                inception.Mixed_7c,
                nn.AdaptiveAvgPool2d(output_size=(1, 1))
            ]
            self.blocks.append(nn.Sequential(*block3))

        for param in self.parameters():
            param.requires_grad = requires_grad

    def forward(self, inp):
        """Get Inception feature maps
        Parameters
        ----------
        inp : torch.autograd.Variable
            Input tensor of shape Bx3xHxW. Values are expected to be in
            range (0, 1)
        Returns
        -------
        List of torch.autograd.Variable, corresponding to the selected output
        block, sorted ascending by index
        """
        outp = []
        x = inp

        if self.resize_input:
            x = F.upsample(x, size=(299, 299), mode='bilinear')

        if self.normalize_input:
            x = x.clone()
            x[:, 0] = x[:, 0] * (0.229 / 0.5) + (0.485 - 0.5) / 0.5
            x[:, 1] = x[:, 1] * (0.224 / 0.5) + (0.456 - 0.5) / 0.5
            x[:, 2] = x[:, 2] * (0.225 / 0.5) + (0.406 - 0.5) / 0.5

        for idx, block in enumerate(self.blocks):
            x = block(x)
            if idx in self.output_blocks:
                outp.append(x)

            if idx == self.last_needed_block:
                break

        return outp

class FID():
    """docstring for FID
    Calculates the Frechet Inception Distance (FID) to evalulate GANs
    The FID metric calculates the distance between two distributions of images.
    Typically, we have summary statistics (mean & covariance matrix) of one
    of these distributions, while the 2nd distribution is given by a GAN.
    When run as a stand-alone program, it compares the distribution of
    images that are stored as PNG/JPEG at a specified location with a
    distribution given by summary statistics (in pickle format).
    The FID is calculated by assuming that X_1 and X_2 are the activations of
    the pool_3 layer of the inception net for generated samples and real world
    samples respectivly.
    See --help to see further details.
    Code apapted from https://github.com/bioinf-jku/TTUR to use PyTorch instead
    of Tensorflow
    Copyright 2018 Institute of Bioinformatics, JKU Linz
    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at
       http://www.apache.org/licenses/LICENSE-2.0
    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
    """
    def __init__(self):
        self.dims = 2048
        self.batch_size = 64
        self.cuda = True
        self.verbose=False

        block_idx = InceptionV3.BLOCK_INDEX_BY_DIM[self.dims]
        self.model = InceptionV3([block_idx])
        if self.cuda:
            # TODO: put model into specific GPU
            self.model.cuda()

    def __call__(self, images, gt_path):
        """ images:  list of the generated image. The values must lie between 0 and 1.
            gt_path: the path of the ground truth images.  The values must lie between 0 and 1.
        """
        if not os.path.exists(gt_path):
            raise RuntimeError('Invalid path: %s' % gt_path)


        print('calculate gt_path statistics...')
        m1, s1 = self.compute_statistics_of_path(gt_path, self.verbose)
        print('calculate generated_images statistics...')
        m2, s2 = self.calculate_activation_statistics(images, self.verbose)
        fid_value = self.calculate_frechet_distance(m1, s1, m2, s2)
        return fid_value


    def calculate_from_disk(self, generated_path, gt_path):
        """
        """
        if not os.path.exists(gt_path):
            raise RuntimeError('Invalid path: %s' % gt_path)
        if not os.path.exists(generated_path):
            raise RuntimeError('Invalid path: %s' % generated_path)

        print('calculate gt_path statistics...')
        
        
        if gt_path=='/data2/xueqing_tong/dataset/cropped_front/resize256/train':
            m1=np.load("m1_cropped_front_256_train.npy")
            s1=np.load("s1_cropped_front_256_train.npy")
        elif gt_path=='/data2/xueqing_tong/dataset/cropped_side/resize256/train':
            m1=np.load("m1_cropped_side_256_train.npy")
            s1=np.load("s1_cropped_side_256_train.npy")
            # m1, s1 = self.compute_statistics_of_path(gt_path, self.verbose)
            # np.save('m1_cropped_side_256_train.npy',m1)
            # np.save('s1_cropped_side_256_train.npy',s1)
        elif gt_path=='/data2/xueqing_tong/dataset/cropped_side/resize512/image_cropped_side/train':
            m1=np.load("m1_cropped_side_512_train.npy")
            s1=np.load("s1_cropped_side_512_train.npy")
            # m1, s1 = self.compute_statistics_of_path(gt_path, self.verbose)
            # np.save('m1_cropped_side_512_train.npy',m1)
            # np.save('s1_cropped_side_512_train.npy',s1)
        elif gt_path=='/data2/xueqing_tong/dataset/cropped_front/resize512/train':
            m1=np.load("m1_cropped_front_512_train.npy")
            s1=np.load("s1_cropped_front_512_train.npy")
            # m1, s1 = self.compute_statistics_of_path(gt_path, self.verbose)
            # np.save('m1_cropped_front_512_train.npy',m1)
            # np.save('s1_cropped_front_512_train.npy',s1)
        else:
            m1, s1 = self.compute_statistics_of_path(gt_path, self.verbose)

        print('calculate generated_path statistics...')
        m2, s2 = self.compute_statistics_of_path(generated_path, self.verbose)
        print('calculate frechet distance...')
        fid_value = self.calculate_frechet_distance(m1, s1, m2, s2)
        print('fid_distance %f' % (fid_value))
        return fid_value


    def compute_statistics_of_path(self, path, verbose):
        # npz_file = os.path.join(path, 'statistics.npz')
        # if os.path.exists(npz_file):
        #     f = np.load(npz_file)
        #     m, s = f['mu'][:], f['sigma'][:]
        #     f.close()
        # else:
        m, s = self.calculate_activation_statistics(path, verbose)
            # np.savez(npz_file, mu=m, sigma=s)

        return m, s

    def calculate_activation_statistics(self, path, verbose):
        """Calculation of the statistics used by the FID.
        Params:
        -- images      : Numpy array of dimension (n_images, 3, hi, wi). The values
                         must lie between 0 and 1.
        -- model       : Instance of inception model
        -- batch_size  : The images numpy array is split into batches with
                         batch size batch_size. A reasonable batch size
                         depends on the hardware.
        -- dims        : Dimensionality of features returned by Inception
        -- cuda        : If set to True, use GPU
        -- verbose     : If set to True and parameter out_step is given, the
                         number of calculated batches is reported.
        Returns:
        -- mu    : The mean over samples of the activations of the pool_3 layer of
                   the inception model.
        -- sigma : The covariance matrix of the activations of the pool_3 layer of
                   the inception model.
        """
        act = self.get_activations(path, verbose)
        mu = np.mean(act, axis=0)
        sigma = np.cov(act, rowvar=False)
        return mu, sigma



    def get_activations(self, path, verbose=False):
        """Calculates the activations of the pool_3 layer for all images.
        Params:
        -- images      : Numpy array of dimension (n_images, 3, hi, wi). The values
                         must lie between 0 and 1.
        -- model       : Instance of inception model
        -- batch_size  : the images numpy array is split into batches with
                         batch size batch_size. A reasonable batch size depends
                         on the hardware.
        -- dims        : Dimensionality of features returned by Inception
        -- cuda        : If set to True, use GPU
        -- verbose     : If set to True and parameter out_step is given, the number
                         of calculated batches is reported.
        Returns:
        -- A numpy array of dimension (num images, dims) that contains the
           activations of the given tensor when feeding inception with the
           query tensor.
        """
        self.model.eval()

        path = pathlib.Path(path)
        filenames = list(path.glob('*.jpg')) + list(path.glob('*.png'))
        # filenames = os.listdir(path)
        d0 = len(filenames)

        n_batches = d0 // self.batch_size
        n_used_imgs = n_batches * self.batch_size
        import tqdm
        pred_arr = np.empty((n_used_imgs, self.dims))
        for i in tqdm.tqdm(range(n_batches)):

            start = i * self.batch_size
            end = start + self.batch_size

            imgs = np.array([imread(str(fn)).astype(np.float32) for fn in filenames[start:end]])

            # Bring images to shape (B, 3, H, W)
            imgs = imgs.transpose((0, 3, 1, 2))

            # Rescale images to be between 0 and 1
            imgs /= 255

            batch = torch.from_numpy(imgs).type(torch.FloatTensor)
            # batch = Variable(batch, volatile=True)
            if self.cuda:
                batch = batch.cuda()

            pred = self.model(batch)[0]

            # If model output is not scalar, apply global spatial average pooling.
            # This happens if you choose a dimensionality not equal 2048.
            if pred.shape[2] != 1 or pred.shape[3] != 1:
                pred = adaptive_avg_pool2d(pred, output_size=(1, 1))

            pred_arr[start:end] = pred.cpu().data.numpy().reshape(self.batch_size, -1)

        if verbose:
            print(' done')

        return pred_arr


    def calculate_frechet_distance(self, mu1, sigma1, mu2, sigma2, eps=1e-6):
        """Numpy implementation of the Frechet Distance.
        The Frechet distance between two multivariate Gaussians X_1 ~ N(mu_1, C_1)
        and X_2 ~ N(mu_2, C_2) is
                d^2 = ||mu_1 - mu_2||^2 + Tr(C_1 + C_2 - 2*sqrt(C_1*C_2)).
        Stable version by Dougal J. Sutherland.
        Params:
        -- mu1   : Numpy array containing the activations of a layer of the
                   inception net (like returned by the function 'get_predictions')
                   for generated samples.
        -- mu2   : The sample mean over activations, precalculated on an
                   representive data set.
        -- sigma1: The covariance matrix over activations for generated samples.
        -- sigma2: The covariance matrix over activations, precalculated on an
                   representive data set.
        Returns:
        --   : The Frechet Distance.
        """

        mu1 = np.atleast_1d(mu1)
        mu2 = np.atleast_1d(mu2)

        sigma1 = np.atleast_2d(sigma1)
        sigma2 = np.atleast_2d(sigma2)

        assert mu1.shape == mu2.shape, \
            'Training and test mean vectors have different lengths'
        assert sigma1.shape == sigma2.shape, \
            'Training and test covariances have different dimensions'

        diff = mu1 - mu2

        # Product might be almost singular
        covmean, _ = linalg.sqrtm(sigma1.dot(sigma2), disp=False)
        if not np.isfinite(covmean).all():
            msg = ('fid calculation produces singular product; '
                   'adding %s to diagonal of cov estimates') % eps
            print(msg)
            offset = np.eye(sigma1.shape[0]) * eps
            covmean = linalg.sqrtm((sigma1 + offset).dot(sigma2 + offset))

        # Numerical error might give slight imaginary component
        if np.iscomplexobj(covmean):
            if not np.allclose(np.diagonal(covmean).imag, 0, atol=1e-3):
                m = np.max(np.abs(covmean.imag))
                raise ValueError('Imaginary component {}'.format(m))
            covmean = covmean.real

        tr_covmean = np.trace(covmean)

        return (diff.dot(diff) + np.trace(sigma1) +
                np.trace(sigma2) - 2 * tr_covmean)

def get_image_list(flist):
    if isinstance(flist, list):
        return flist

    # flist: image file path, image directory path, text file flist path
    if isinstance(flist, str):
        if os.path.isdir(flist):
            flist = list(glob.glob(flist + '/*.jpg')) + list(glob.glob(flist + '/*.png'))
            flist.sort()
            return flist

        if os.path.isfile(flist):
            try:
                return np.genfromtxt(flist, dtype=np.str)
            except:
                return [flist]
    print('can not read files from %s return empty list'%flist)
    return []

def crop_img(path):
    bname = os.path.basename(path)
    save_dir = path.split(bname)[0]+bname+'_crop'
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)
    for item in os.listdir(path):
        if not item.endswith('.jpg') and not item.endswith('.png'):
            continue
        img = Image.open(os.path.join(path, item))
        imgcrop = img.crop((40, 0, 216, 256))
        imgcrop.save(os.path.join(save_dir, item))
    return save_dir

class LPIPS():
    def __init__(self, use_gpu=True):
        self.model =lpips.LPIPS(net='alex').cuda()
        self.use_gpu=use_gpu

    def __call__(self, image_1, image_2):
        """
            image_1: images with size (n, 3, w, h) with value [-1, 1]
            image_2: images with size (n, 3, w, h) with value [-1, 1]
        """
        result = self.model.forward(image_1, image_2)
        return result

    def calculate_from_disk(self, path_1, path_2, batch_size=64, verbose=False, sort=True):
        if sort:
            files_1 = sorted(get_image_list(path_1))
            files_2 = sorted(get_image_list(path_2))
        else:
            files_1 = get_image_list(path_1)
            files_2 = get_image_list(path_2)
        
        result=[]

        d0 = len(files_1)
        assert len(files_1)  == len(files_2)
        for _ in range(len(files_1)):
            if files_1[_].split('/')[-1].replace('___',"__") !=files_2[_].split('/')[-1]:
                raise Exception('file not match')

        if batch_size > d0:
            print(('Warning: batch size is bigger than the data size. '
                   'Setting batch size to data size'))
            batch_size = d0

        n_batches = d0 // batch_size
        n_used_imgs = n_batches * batch_size

        for i in tqdm.tqdm(range(n_batches)):
            if verbose:
                print('\rPropagating batch %d/%d' % (i + 1, n_batches))
                # end='', flush=True)
            start = i * batch_size
            end = start + batch_size
            imgs_1 = np.array([imread(str(fn)).astype(np.float32) / 127.5 - 1 for fn in files_1[start:end]])
            imgs_2 = np.array([imread(str(fn)).astype(np.float32) / 127.5 - 1 for fn in files_2[start:end]])

            # Bring images to shape (B, 3, H, W)
            imgs_1 = imgs_1.transpose((0, 3, 1, 2))
            imgs_2 = imgs_2.transpose((0, 3, 1, 2))
            img_1_batch = torch.from_numpy(imgs_1).type(torch.FloatTensor)
            img_2_batch = torch.from_numpy(imgs_2).type(torch.FloatTensor)

            if self.use_gpu:
                img_1_batch = img_1_batch.cuda()
                img_2_batch = img_2_batch.cuda()
            result.append(self.model.forward(img_1_batch, img_2_batch).detach().cpu().numpy())
        distance = np.average(result)
        sub=np.array([int(i.split('/')[-1].split('_')[0]) for i in files_1][:n_used_imgs])
        result_s=np.concatenate(result,axis=0)
        print('lpips: %.4f'%distance)
        # for i in range(81,101):
        #     x=(result_s[sub==i]).mean()
        #     print(f"id:{i} lpips{x}")
        return distance

    def calculate_mask_lpips(self, distorated_path, fid_real_path,seg_path, batch_size=64, verbose=False,sort=False):
        if sort:
            files_1 = sorted(get_image_list(distorated_path))
            files_2 = sorted(get_image_list(fid_real_path))
            files_3 = sorted(get_image_list(seg_path))
        else:
            files_1 = get_image_list(distorated_path)
            files_2 = get_image_list(fid_real_path)
            files_3 = get_image_list(seg_path)
        result=[]
        
        d0 = len(files_1)
        assert len(files_1)  == len(files_2)
        assert len(files_1)  == len(files_3)
        for _ in range(len(files_1)):
            if files_1[_].split('/')[-1].replace('___',"__") !=files_2[_].split('/')[-1]:
            # if files_1[_].split('/')[-1] !=files_2[_].split('/')[-1]:
                raise Exception('file not match')

        if batch_size > d0:
            print(('Warning: batch size is bigger than the data size. '
                   'Setting batch size to data size'))
            batch_size = d0

        n_batches = d0 // batch_size
        for i in tqdm.tqdm(range(n_batches)):
            if verbose:
                print('\rPropagating batch %d/%d' % (i + 1, n_batches))
            start = i * batch_size
            end = start + batch_size
            imgs_1 = np.array([imread(str(fn)).astype(np.float32) / 127.5 - 1 for fn in files_1[start:end]])
            imgs_2 = np.array([imread(str(fn)).astype(np.float32) / 127.5 - 1 for fn in files_2[start:end]])
            imgs_3 = np.array([imread(str(fn)).astype(np.float32) for fn in files_3[start:end]])
            imgs_1=imgs_1*(imgs_3!=0)
            imgs_2=imgs_2*(imgs_3!=0)


            # Bring images to shape (B, 3, H, W)
            imgs_1 = imgs_1.transpose((0, 3, 1, 2))
            imgs_2 = imgs_2.transpose((0, 3, 1, 2))

            img_1_batch = torch.from_numpy(imgs_1).type(torch.FloatTensor)
            img_2_batch = torch.from_numpy(imgs_2).type(torch.FloatTensor)

            if self.use_gpu:
                img_1_batch = img_1_batch.cuda()
                img_2_batch = img_2_batch.cuda()

            result.append(self.model.forward(img_1_batch, img_2_batch).detach().cpu().numpy())

        distance = np.average(result)
        print('masked lpips: %.4f'%distance)
        return distance
    def calculate_mask_lpips_face(self, distorated_path, fid_real_path,seg_path, batch_size=64, verbose=False,sort=False):
        if sort:
            files_1 = sorted(get_image_list(distorated_path))
            files_2 = sorted(get_image_list(fid_real_path))
            files_3 = sorted(get_image_list(seg_path))
        else:
            files_1 = get_image_list(distorated_path)
            files_2 = get_image_list(fid_real_path)
            files_3 = get_image_list(seg_path)
        result=[]
        
        d0 = len(files_1)
        assert len(files_1)  == len(files_2)
        
        for _ in range(len(files_1)):
            # if files_1[_].split('/')[-1] !=files_2[_].split('/')[-1]:
            if files_1[_].split('/')[-1].replace('___',"__") !=files_2[_].split('/')[-1]:
                raise Exception('file not match')

        if batch_size > d0:
            print(('Warning: batch size is bigger than the data size. '
                   'Setting batch size to data size'))
            batch_size = d0

        n_batches = d0 // batch_size
        for i in tqdm.tqdm(range(n_batches)):
            if verbose:
                print('\rPropagating batch %d/%d' % (i + 1, n_batches))
            start = i * batch_size
            end = start + batch_size
            imgs_1 = np.array([imread(str(fn)).astype(np.float32) / 127.5 - 1 for fn in files_1[start:end]])
            imgs_2 = np.array([imread(str(fn)).astype(np.float32) / 127.5 - 1 for fn in files_2[start:end]])
            imgs_3 = np.array([imread(str(fn)).astype(np.float32) for fn in files_3[start:end]])
            imgs_1=imgs_1*(imgs_3!=13)
            imgs_2=imgs_2*(imgs_3!=13)


            # Bring images to shape (B, 3, H, W)
            imgs_1 = imgs_1.transpose((0, 3, 1, 2))
            imgs_2 = imgs_2.transpose((0, 3, 1, 2))

            img_1_batch = torch.from_numpy(imgs_1).type(torch.FloatTensor)
            img_2_batch = torch.from_numpy(imgs_2).type(torch.FloatTensor)

            if self.use_gpu:
                img_1_batch = img_1_batch.cuda()
                img_2_batch = img_2_batch.cuda()

            result.append(self.model.forward(img_1_batch, img_2_batch).detach().cpu().numpy())

        distance = np.average(result)
        print('masked face lpips: %.4f'%distance)
        return distance

if __name__ == "__main__":
    print('load start')
    lpips = LPIPS()
    print('load LPIPS')

    parser = argparse.ArgumentParser(description='script to compute all statistics')
    parser.add_argument('--gt_path', help='Path to ground truth data', type=str)
    parser.add_argument('--distorated_path', help='Path to output data', type=str)
    parser.add_argument('--fid_real_path', help='Path to real images when calculate FID', type=str)
    parser.add_argument('--seg_path',help='Path to seg path',type=str)
    args = parser.parse_args()

    for arg in vars(args):
        print('[%s] =' % arg, getattr(args, arg))
    # args.distorated_path = crop_img(args.distorated_path)
    # args.gt_path = crop_img(args.gt_path)

    fid = FID()
    print('load FID')

    print('calculate fid metric...')
    fid_score = fid.calculate_from_disk(args.distorated_path, args.fid_real_path)

    print('calculate lpips metric...')
    lpips_score = lpips.calculate_from_disk(args.distorated_path, args.gt_path, sort=False)

    print('calculate masked lpips and SSIM metric...')
    lpips_masked = lpips.calculate_mask_lpips(args.distorated_path, args.gt_path,args.seg_path)

    # print('calculate masked-face lpips and SSIM metric...')
    # lpips_masked_face = lpips.calculate_mask_lpips_face(args.distorated_path, args.gt_path,args.seg_path)

