import glob
import random
from io import BytesIO
from itertools import chain
from multiprocessing.dummy import Pool as ThreadPool

import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision.transforms.functional import to_tensor
from torch import nn


use_cuda = torch.cuda.is_available()
FloatTensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor
LongTensor = torch.cuda.LongTensor if use_cuda else torch.LongTensor
Tensor = FloatTensor


class ImageData(Dataset):
    def __init__(self,
                 img_folder,
                 max_patch_per_img,
                 patch_size,
                 shrink_size,
                 noise_level,
                 down_sample_method=None,
                 color_mod='RGB'):

        self.img_folder = img_folder
        self.max_path_per_img = max_patch_per_img
        self.patch_size = patch_size
        self.color_mod = color_mod
        self.img_augmenter = ImageAugment(shrink_size, noise_level, down_sample_method)
        self.patch_grids = self.get_img_patch_grids()

    def get_img_patch_grids(self):
        file_names = glob.glob(self.img_folder + '**/*.png', recursive=True)

        print("Pre-processing all images into patches.")
        pool = ThreadPool(4)
        patch_grids = pool.map(self.get_img_patches, file_names)
        pool.close()
        pool.join()

        # patch_grids = list(map(self.get_img_patches, file_names))
        patch_grids = list(chain.from_iterable(patch_grids))
        print("Find {} patches from {} images.".format(len(patch_grids), len(file_names)))
        return patch_grids

    def get_img_patches(self, img_file):
        img = Image.open(img_file).convert("RGB")
        img_grids = self.get_img_grids(img)
        lr_hr_patches = [self.img_augmenter.process(img, grid) for grid in img_grids]
        img.close()
        return lr_hr_patches

    def get_img_grids(self, img):
        # return nested list [ [img_file, position tuple], ...]
        img_w, img_h = img.size
        count_w, count_h = img_w // self.patch_size, img_h // self.patch_size
        patch_box = []
        for i in range(count_h):
            for j in range(count_w):
                if self.patch_size * (j + 1) <= img_h and self.patch_size * (i + 1) <= img_w:
                    patch_box.append((self.patch_size * i,
                                      self.patch_size * j,
                                      self.patch_size * (i + 1),
                                      self.patch_size * (j + 1)))

        if len(patch_box) > self.max_path_per_img:
            patch_box = random.sample(patch_box, self.max_path_per_img)

        return patch_box

    def __len__(self):
        return len(self.patch_grids)

    def __getitem__(self, index):
        patch = self.patch_grids[index]
        if self.color_mod == 'RGB':
            lr_img = patch[0].convert("RGB")
            hr_img = patch[1].convert("RGB")
        elif self.color_mod == 'YCbCr':
            lr_img, _, _ = patch[0].convert('YCbCr').split()
            hr_img, _, _ = patch[1].convert('YCbCr').split()
        else:
            raise KeyError('Either RGB or YCbCr')
        return [lr_img, hr_img]


class ImageAugment:
    def __init__(self,
                 shrink_size=2,
                 noise_level=1,
                 down_sample_method=None
                 ):
        # noise_level (int): 0: no noise; 1: 75-95% quality; 2:50-75%
        if noise_level == 0:
            self.noise_level = [0, 0]
        elif noise_level == 1:
            self.noise_level = [5, 25]
        elif noise_level == 2:
            self.noise_level = [25, 50]
        else:
            raise KeyError("Noise level should be either 0, 1, 2")
        self.shrink_size = shrink_size
        self.down_sample_method = down_sample_method

    def shrink_img(self, hr_img):

        if self.down_sample_method is None:
            resample_method = random.choice([Image.BILINEAR, Image.BICUBIC, Image.LANCZOS])
        else:
            resample_method = self.down_sample_method
        img_w, img_h = tuple(map(lambda x: int(x / self.shrink_size), hr_img.size))
        lr_img = hr_img.resize((img_w, img_h), resample_method)
        return lr_img

    def add_jpeg_noise(self, hr_img):
        quality = 100 - round(random.uniform(*self.noise_level))
        lr_img = BytesIO()
        hr_img.save(lr_img, format='JPEG', quality=quality)
        lr_img.seek(0)
        lr_img = Image.open(lr_img)
        return lr_img

    def process(self, hr_patch, grid):
        hr_patch = hr_patch.crop(grid)
        lr_patch = self.shrink_img(hr_patch)
        if self.noise_level[1] > 0:
            lr_patch = self.add_jpeg_noise(lr_patch)

        return lr_patch, hr_patch

    def up_sample(self, img, resample):
        width, height = img.size
        return img.resize((self.shrink_size * width, self.shrink_size * height), resample=resample)


class ImageLoader(DataLoader):
    def __init__(self, dataset, up_sample=Image.BILINEAR,
                 batch_size=1, shuffle=True, pad_img=0):
        self.dataset = dataset
        self.up_sample = up_sample
        self.pad_img = pad_img
        super(ImageLoader, self).__init__(dataset,
                                          batch_size,
                                          shuffle,
                                          collate_fn=self.batch_collector)

    def batch_collector(self, batch):
        lr_hr_patch = batch
        lr_img = [to_tensor(i[0]) for i in lr_hr_patch]
        lr_img = torch.stack(lr_img, dim=0).contiguous()

        hr_img = [to_tensor(i[1]) for i in lr_hr_patch]
        hr_img = torch.stack(hr_img, dim=0).contiguous()
        if self.pad_img:
            lr_img = nn.ZeroPad2d(self.pad_img)(lr_img)
        if use_cuda:
            lr_img = lr_img.cuda()
            hr_img = hr_img.cuda(async=True)

        if self.up_sample is not None:
            lr_img_up = [self.dataset.img_augmenter.up_sample(i[0], self.up_sample) for i in lr_hr_patch]
            lr_img_up = [to_tensor(i) for i in lr_img_up]
            lr_img_up = torch.stack(lr_img_up, dim=0).contiguous()
            if use_cuda:
                lr_img_up = lr_img_up.cuda(async=True)
            lr_img = (lr_img, lr_img_up)
        return lr_img, hr_img


if __name__ == '__main__':
    train_folder = './dataset/train/'
    # img_dataset = ImageData(img_folder=train_folder,
    #                         max_patch_per_img=1000,
    #                         patch_size=611,
    #                         shrink_size=2,
    #                         noise_level=1,
    #                         down_sample_method=Image.BICUBIC)
    #
    # img_data = ImageLoader(img_dataset, batch_size=10, shuffle=True)
    #
    # for i, patch in enumerate(img_data):
    #     lr, hr = patch
    #     save_image(lr, "./dataset/temp/lr_{}.jpeg".format(i), padding=0, nrow=1)
    #     save_image(hr, "./dataset/temp/hr_{}.jpeg".format(i), padding=0, nrow=1)
