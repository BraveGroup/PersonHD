from __future__ import division, print_function
import os
import sys
sys.path.append('.')

import torch
import tensorboardX
from data.data_loader import CreateDataLoader
from models.pose_transfer_model import PoseTransferModel
from options.pose_transfer_options import TrainPoseTransferOptions
from util.visualizer import Visualizer
from util.loss_buffer import LossBuffer

import util.io as io
import tqdm
import time
from collections import OrderedDict

# parse and save options
parser = TrainPoseTransferOptions()
opt = parser.parse()
parser.save()
# create model
model = PoseTransferModel()
model.initialize(opt)
# save terminal line
io.save_str_list([' '.join(sys.argv)], os.path.join(model.save_dir, 'order_line.txt'))
# create data loader
train_loader = CreateDataLoader(opt, split='train')
val_loader = CreateDataLoader(opt, split='test' if not opt.small_val_set else 'test_small')
# create visualizer
visualizer = Visualizer(opt)

logdir = os.path.join('logs', opt.id)
if not os.path.exists(logdir):
    os.makedirs(logdir)
writer = tensorboardX.SummaryWriter(logdir)

# set "saving best"
best_info = {
    'meas': 'SSIM',
    'type': 'max',
    'best_value': 0,
    'best_epoch': -1
}

# set continue training
if not opt.resume_train:
    total_steps = 0
    epoch_count = 1
else:
    last_epoch = int(opt.last_epoch)
    total_steps = len(train_loader)*last_epoch
    epoch_count = 1 + last_epoch

if opt.debug:
    opt.display_freq = 2

for epoch in tqdm.trange(epoch_count, opt.n_epoch+opt.n_epoch_decay+1, desc='Epoch'):
    #train model
    model.train()
    model.netG.train()
    model.netD.train()
    if model.opt.G_pix_warp:
        model.netPW.train()

    model.use_gan = (opt.loss_weight_gan > 0) and (epoch >= opt.epoch_add_gan)
    for i,data in enumerate(tqdm.tqdm(train_loader, desc='Train')):
        total_steps += 1
        model.set_input(data)
        model.optimize_parameters(check_grad=(opt.check_grad_freq>0 and total_steps%opt.check_grad_freq==0))
        
        if total_steps % opt.display_freq == 0:
            train_error = model.get_current_errors()
            info = OrderedDict([
                    ('id', opt.id),
                    ('iter', total_steps),
                    ('epoch', epoch),
                    ('lr', model.optimizers[0].param_groups[0]['lr']),
                ])
            tqdm.tqdm.write(visualizer.log(info, train_error))
            for k, v in train_error.items():
                writer.add_scalar(k, v, total_steps)
            writer.add_scalar('lr', model.optimizers[0].param_groups[0]['lr'], total_steps)
            writer.flush()

    #update learning rate(lr_scheduler.step()) after optim.step(), otherwise lost first lr
    model.update_learning_rate()    

    if epoch % opt.test_epoch_freq == 0:
        # model.get_current_errors() #erase training error information
        model.output = {}
        loss_buffer = LossBuffer(size=len(val_loader))
        #eval model
        model.netG.eval()
        model.eval()
        if model.opt.G_pix_warp:
            model.netPW.eval()

        for i, data in enumerate(tqdm.tqdm(val_loader, desc='Test')):
            model.set_input(data)
            model.test(compute_loss=True)
            loss_buffer.add(model.get_current_errors())
        test_error = loss_buffer.get_errors()
        info = OrderedDict([
                ('time', time.ctime()),
                ('id', opt.id),
                ('epoch', epoch),
        ])
        tqdm.tqdm.write(visualizer.log(info, test_error))
        # save best
        if best_info['best_epoch']==-1 or (test_error[best_info['meas']].item()<best_info['best_value'] and best_info['type']=='min') or (test_error[best_info['meas']].item()>best_info['best_value'] and best_info['type']=='max'):
            tqdm.tqdm.write('save as best epoch!')
            best_info['best_epoch'] = epoch
            best_info['best_value'] = test_error[best_info['meas']].item()
            model.save('best')
        tqdm.tqdm.write(visualizer.log(best_info))
    
    if epoch % opt.vis_epoch_freq == 0:
        #eval model
        model.eval()
        model.netG.eval()
        if model.opt.G_pix_warp:
            model.netPW.eval()

        num_vis_batch = int(1.*opt.n_vis/opt.batch_size)
        visuals = None
        for i, data in enumerate(train_loader):
            if i == num_vis_batch:
                break
            model.set_input(data)
            model.test(compute_loss=True)
            v = model.get_current_visuals()
            if visuals is None:
                visuals = v
            else:
                for name, item in v.items():
                    visuals[name][0] = torch.cat((visuals[name][0], item[0]), dim=0)
        tqdm.tqdm.write('visualizing training sample')
        fn_vis = os.path.join('checkpoints', opt.id, 'vis', 'train_epoch%d.jpg'%epoch)
        visualizer.visualize_results(visuals, fn_vis)

        visuals = None
        for i, data in enumerate(val_loader):
            if i == num_vis_batch:
                break
            model.set_input(data)
            model.test(compute_loss=True)
            v = model.get_current_visuals()
            if visuals is None:
                visuals = v
            else:
                for name, item in v.items():
                    visuals[name][0] = torch.cat((visuals[name][0], item[0]), dim=0)
        tqdm.tqdm.write('visualizing test sample')
        fn_vis = os.path.join('checkpoints', opt.id, 'vis', 'test_epoch%d.jpg'%epoch)
        visualizer.visualize_results(visuals, fn_vis)
    
    if epoch % opt.save_epoch_freq == 0:
        model.save(epoch)
    model.save('latest')
print(best_info)
