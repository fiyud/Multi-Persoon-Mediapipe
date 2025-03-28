#! /usr/bin/env python
#! coding:utf-8
from pathlib import Path
import matplotlib.pyplot as plt
from torch import log
from tqdm import tqdm
import torch
import torch.nn as nn
import argparse
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
from sklearn.metrics import confusion_matrix

from dataloader.jhmdb_loader import load_jhmdb_data, Jdata_generator, JConfig
from dataloader.shrec_loader import load_shrec_data, Sdata_generator, SConfig
from models.DDNet_Original import DDNet_Original as DDNet
from mm import DDNet_GCN
from mm1 import DDNet_GCN_GRU

from utils import makedir
import sys
import time
import numpy as np
import logging
sys.path.insert(0, './pytorch-summary/torchsummary/')
from torchsummary import summary  # noqa

savedir = Path('experiments') / Path(str(int(time.time())))
makedir(savedir)
logging.basicConfig(filename=savedir/'train.log', level=logging.INFO)
history = {
    "train_loss": [],
    "test_loss": [],
    "test_acc": []
}

def train(args, model, device, train_loader, optimizer, epoch, criterion):
    # print(f"\n==== TRAINING DEVICE CHECK ====")
    # print(f"Current device in train function: {device}")
    # print(f"Model device in train: {next(model.parameters()).device}")
    # print("================================\n")
    
    model.train()
    train_loss = 0
    # For the first batch only, print device info:
    for batch_idx, (data1, data2, target) in enumerate(tqdm(train_loader)):
        if batch_idx == 0:
            # print(f"\n==== BATCH DEVICE CHECK ====")
            # print(f"Input tensor device before moving: {data1.device}")
            M, P, target = data1.to(device), data2.to(device), target.to(device)
            # print(f"Input tensor device after moving: {M.device}")
            # print(f"Model device during forward: {next(model.parameters()).device}")
            # print("============================\n")
        else:
            M, P, target = data1.to(device), data2.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(M, P)
        loss = criterion(output, target)
        train_loss += loss.detach().item()
        loss.backward()
        optimizer.step()
        if batch_idx % args.log_interval == 0:
            msg = ('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data1), len(train_loader.dataset),
                100. * batch_idx / len(train_loader), loss.item()))
            print(msg)
            logging.info(msg)
            if args.dry_run:
                break
    history['train_loss'].append(train_loss)
    return train_loss

def test(model, device, test_loader):
    model.eval()
    test_loss = 0
    correct = 0
    criterion = nn.CrossEntropyLoss(reduction='sum')
    with torch.no_grad():
        for _, (data1, data2, target) in enumerate(tqdm(test_loader)):
            M, P, target = data1.to(device), data2.to(device), target.to(device)
            output = model(M, P)
            # sum up batch loss
            test_loss += criterion(output, target).item()
            # get the index of the max log-probability
            pred = output.argmax(dim=1, keepdim=True)
            # output shape (B,Class)
            # target_shape (B)
            # pred shape (B,1)
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)
    history['test_loss'].append(test_loss)
    history['test_acc'].append(correct / len(test_loader.dataset))
    msg = ('Test set: Average loss: {:.4f}, Accuracy: {}/{} ({:.2f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))
    print(msg)
    logging.info(msg)

def main():
    # Training settings
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=199, metavar='N',
                        help='number of epochs to train (default: 199)')
    parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                        help='learning rate (default: 0.01)')
    parser.add_argument('--gamma', type=float, default=0.5, metavar='M',
                        help='Learning rate step gamma (default: 0.5)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='quickly check a single pass')
    parser.add_argument('--log-interval', type=int, default=2, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--save-model', action='store_true', default=True,
                        help='For Saving the current Model')
    parser.add_argument('--dataset', type=int, required=True, metavar='N',
                        help='0 for JHMDB, 1 for SHREC coarse, 2 for SHREC fine, others is undefined')
    parser.add_argument('--model', action='store_true', default=False,
                        help='For Saving the current Model')
    # parser.add_argument('--a', default=1,
    #                     help='For Saving the current Model')
    parser.add_argument('--calc_time', action='store_true', default=False,
                        help='calc calc time per sample')
    args = parser.parse_args()
    logging.info(args)
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    device = torch.device("cuda" if use_cuda else "cpu")
    # print("Device: ", device)
    if torch.cuda.is_available():
    # Create a small tensor and operation to initialize CUDA context
        dummy = torch.ones(1).cuda()
        result = dummy + 1
        print(f"CUDA initialization test - tensor is on: {result.device}")
        del dummy, result
        torch.cuda.empty_cache()
    # kwargs = {'batch_size': args.batch_size}
    # if use_cuda:
    #     kwargs.update({'num_workers': 1,
    #                    'pin_memory': True,
    #                    'shuffle': True,
    #                    'generator': torch.Generator('cuda')},)

    kwargs = {'batch_size': args.batch_size}
    if use_cuda:
        kwargs.update({
            'num_workers': 0,  # Try with 0 workers first to eliminate that variable
            'pin_memory': True,
            'shuffle': True,
            # Use a CPU generator (no device specified)
            'generator': torch.Generator().manual_seed(42)
        })
    # alias
    Config = None
    data_generator = None
    load_data = None
    clc_num = 0
    if args.dataset == 0:
        Config = JConfig()
        data_generator = Jdata_generator
        load_data = load_jhmdb_data
        clc_num = Config.clc_num
    elif args.dataset == 1:
        Config = SConfig()
        load_data = load_shrec_data
        clc_num = Config.class_coarse_num
        data_generator = Sdata_generator('coarse_label')
    elif args.dataset == 2:
        Config = SConfig()
        clc_num = Config.class_fine_num
        load_data = load_shrec_data
        data_generator = Sdata_generator('fine_label')
    else:
        print("Unsupported dataset!")
        sys.exit(1)

    C = Config
    Train, Test, le = load_data()
    X_0, X_1, Y = data_generator(Train, C, le)
    X_0 = torch.from_numpy(X_0).type('torch.FloatTensor')
    X_1 = torch.from_numpy(X_1).type('torch.FloatTensor')
    Y = torch.from_numpy(Y).type('torch.LongTensor')

    X_0_t, X_1_t, Y_t = data_generator(Test, C, le)
    X_0_t = torch.from_numpy(X_0_t).type('torch.FloatTensor')
    X_1_t = torch.from_numpy(X_1_t).type('torch.FloatTensor')
    Y_t = torch.from_numpy(Y_t).type('torch.LongTensor')

    trainset = torch.utils.data.TensorDataset(X_0, X_1, Y)
    train_loader = torch.utils.data.DataLoader(trainset, **kwargs)

    testset = torch.utils.data.TensorDataset(X_0_t, X_1_t, Y_t)
    test_loader = torch.utils.data.DataLoader(
        testset, batch_size=args.test_batch_size)

    def debug_cuda_status():
        print("\n==== CUDA DEBUG INFORMATION ====")
        print(f"PyTorch version: {torch.__version__}")
        print(f"CUDA available: {torch.cuda.is_available()}")
    
    if torch.cuda.is_available():
        print(f"CUDA version: {torch.version.cuda}")
        print(f"Current device: {torch.cuda.current_device()}")
        print(f"Device count: {torch.cuda.device_count()}")
        print(f"Device name: {torch.cuda.get_device_name(0)}")
        print(f"Device properties: {torch.cuda.get_device_properties(0)}")
        print(f"Memory allocated: {torch.cuda.memory_allocated(0) / 1024**2:.2f} MB")
        print(f"Memory reserved: {torch.cuda.memory_reserved(0) / 1024**2:.2f} MB")
    print("=================================\n")

    debug_cuda_status()

    # Modify your model instantiation code to add explicit device checks:
    # if args.a == 1:
    #     Net = DDNet(C.frame_l, C.joint_n, C.joint_d, C.feat_d, C.filters, clc_num)
    # elif args.a == 2:
    Net = DDNet_GCN_GRU(C.frame_l, C.joint_n, C.joint_d, C.feat_d, C.filters, clc_num)

    model = Net.to(device)

    # print(f"\n==== MODEL DEVICE CHECK ====")
    # print(f"Model was moved to: {device}")
    # print(f"First layer device: {next(model.parameters()).device}")
    for name, param in model.named_parameters():
        print(f"Parameter {name} is on {param.device}")
        break  # Just show one parameter as example
    # print("=============================\n")

    # summary(model, [(C.frame_l, C.feat_d), (C.frame_l, C.joint_n, C.joint_d)])
    optimizer = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999))

    criterion = nn.CrossEntropyLoss()
    scheduler = ReduceLROnPlateau(
        optimizer, factor=args.gamma, patience=5, cooldown=0.5, min_lr=5e-6, verbose=True)
    for epoch in range(1, args.epochs + 1):
        train_loss = train(args, model, device, train_loader,
                           optimizer, epoch, criterion)
        test(model, device, test_loader)
        scheduler.step(train_loss)

    fig, (ax1, ax2, ax3) = plt.subplots(nrows=3, ncols=1)
    ax1.plot(history['train_loss'])
    ax1.plot(history['test_loss'])
    ax1.legend(['Train', 'Test'], loc='upper left')
    ax1.set_xlabel('Epoch')
    ax1.set_title('Loss')

    ax2.set_title('Model accuracy')
    ax2.set_ylabel('Accuracy')
    ax2.set_xlabel('Epoch')
    ax2.plot(history['test_acc'])
    xmax = np.argmax(history['test_acc'])
    ymax = np.max(history['test_acc'])
    text = "x={}, y={:.3f}".format(xmax, ymax)
    ax2.annotate(text, xy=(xmax, ymax))

    ax3.set_title('Confusion matrix')
    model.eval()
    with torch.no_grad():
        Y_pred = model(X_0_t.to(device), X_1_t.to(
            device)).cpu().numpy()
    Y_test = Y_t.numpy()
    cnf_matrix = confusion_matrix(
        Y_test, np.argmax(Y_pred, axis=1))
    ax3.imshow(cnf_matrix)
    fig.tight_layout()
    fig.savefig(str(savedir / "perf.png"))
    import os
    if args.save_model:
    # Create the weights directory if it doesn't exist
        weights_dir = "D:\\NCKHSV.2024-2025\\MultiMediapipe\\DD-Net-Pytorch\\weights"
        os.makedirs(weights_dir, exist_ok=True)
        
        # Create a filename for the latest model
        dataset_name = "JHMDB" if args.dataset == 0 else "SHREC_coarse" if args.dataset == 1 else "SHREC_fine"
        
        # Filename includes dataset and final epoch
        filename = f"{dataset_name}_latest_epoch_{args.epochs}.pt"
        
        # Save the model to the specified path
        save_path = os.path.join(weights_dir, filename)
        torch.save(model.state_dict(), save_path)
        print(f"Latest model saved to {save_path}")

    if args.calc_time:
        device_list = ['cpu', 'cuda']  # Changed variable name to avoid overwriting device
        # calc time
        for dev in device_list:  # Use different variable name
            tmp_device = torch.device(dev)
            print(f"\n==== TIMING ON {dev.upper()} ====")
            
            # Store original model device to restore it later
            original_model_device = next(model.parameters()).device
            
            tmp_X_0_t = X_0_t.to(tmp_device)
            tmp_X_1_t = X_1_t.to(tmp_device)
            timing_model = model.to(tmp_device)
            
            print(f"Model moved to {tmp_device} for timing")
            print(f"Input tensors moved to {tmp_device} for timing")
            
            # warm up
            _ = timing_model(tmp_X_0_t, tmp_X_1_t)
            
            if dev == 'cuda':
                torch.cuda.synchronize()  # Make sure GPU operations are complete
            
            tmp_X_0_t = tmp_X_0_t.unsqueeze(1)
            tmp_X_1_t = tmp_X_1_t.unsqueeze(1)
            
            start = time.perf_counter_ns()
            for i in range(tmp_X_0_t.shape[0]):
                _ = timing_model(tmp_X_0_t[i, :, :, :], tmp_X_1_t[i, :, :, :])
                if dev == 'cuda':
                    torch.cuda.synchronize()  # Ensure GPU operations complete
            
            end = time.perf_counter_ns()
            msg = (f"Total {end - start}ns, {(end - start) / (X_0_t.shape[0]):.2f}ns per one on {dev}")
            print(msg)
            logging.info(msg)
            
            # Restore model to original device
            model = model.to(original_model_device)
            print(f"Model restored to {original_model_device}\n")


if __name__ == '__main__':
    main()
