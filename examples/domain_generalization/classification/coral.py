import random
import time
import warnings
import sys
import argparse
import shutil

import torch
import torch.backends.cudnn as cudnn
from torch.optim import SGD
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader
import torchvision.transforms as T
import torch.nn.functional as F

from get_dataset import get_dataset

sys.path.append('../../..')
from dglib.generalization.coral import CorrelationAlignmentLoss
from dglib.modules.sampler import DefaultSampler
from dglib.modules.classifier import ImageClassifier as Classifier
import common.vision.datasets as datasets
import common.vision.models as models
from common.vision.transforms import ResizeImage
from common.utils.data import ForeverDataIterator
from common.utils.metric import accuracy, ConfusionMatrix
from common.utils.meter import AverageMeter, ProgressMeter
from common.utils.logger import CompleteLogger

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def main(args: argparse.Namespace):
    logger = CompleteLogger(args.log, args.phase)
    print(args)

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    cudnn.benchmark = True

    # Data loading code
    normalize = T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    train_transform = T.Compose([
        T.RandomResizedCrop(224, scale=(0.7, 1.0)),
        T.RandomHorizontalFlip(),
        T.ColorJitter(0.3, 0.3, 0.3, 0.3),
        T.RandomGrayscale(),
        T.ToTensor(),
        normalize
    ])
    val_transform = T.Compose([
        ResizeImage(224),
        T.ToTensor(),
        normalize
    ])

    train_dataset, num_classes = get_dataset(dataset_name=args.data, root=args.root, task_list=args.sources,
                                             split='train', download=True, transform=train_transform,
                                             seed=args.seed)
    sampler = DefaultSampler(train_dataset, args.batch_size)
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers,
                              sampler=sampler, drop_last=True)
    val_dataset, _ = get_dataset(dataset_name=args.data, root=args.root, task_list=args.sources, split='val',
                                 download=True, transform=val_transform, seed=args.seed)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    test_dataset, _ = get_dataset(dataset_name=args.data, root=args.root, task_list=args.targets, split='all',
                                  download=True, transform=val_transform, seed=args.seed)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    print("Source Train:", len(train_dataset))
    print('Source Val:', len(val_dataset))
    print("Target:", len(test_dataset))
    train_iter = ForeverDataIterator(train_loader)

    # create model
    print("=> using pre-trained model '{}'".format(args.arch))
    backbone = models.__dict__[args.arch](pretrained=True)
    classifier = Classifier(backbone, num_classes, freeze_bn=args.freeze_bn, dropout_p=args.dropout_p,
                            finetune=args.finetune).to(device)
    num_domains = len(args.sources)

    # define optimizer and lr scheduler
    optimizer = SGD(classifier.get_parameters(base_lr=args.lr), args.lr, momentum=args.momentum, weight_decay=args.wd,
                    nesterov=True)
    lr_scheduler = CosineAnnealingLR(optimizer, args.epochs * args.iters_per_epoch)

    # define loss function
    correlation_alignment_loss = CorrelationAlignmentLoss().to(device)

    if args.phase == 'test':
        checkpoint = torch.load(logger.get_checkpoint_path('best'), map_location='cpu')
        classifier.load_state_dict(checkpoint)
        acc1 = validate(test_loader, classifier, args)
        print(acc1)
        return

    # start training
    best_val_acc1 = 0.
    best_test_acc1 = 0.
    for epoch in range(args.epochs):

        print(lr_scheduler.get_lr())
        # train for one epoch
        train(train_iter, classifier, optimizer, lr_scheduler, correlation_alignment_loss, num_domains, epoch, args)

        # evaluate on validation set
        print("Validation on source domain...")
        acc1 = validate(val_loader, classifier, args)

        # remember best acc@1 and save checkpoint
        torch.save(classifier.state_dict(), logger.get_checkpoint_path('latest'))
        if acc1 > best_val_acc1:
            shutil.copy(logger.get_checkpoint_path('latest'), logger.get_checkpoint_path('best'))
        best_val_acc1 = max(acc1, best_val_acc1)

        # evaluate on test set
        print("Test on target domain...")
        best_test_acc1 = max(best_test_acc1, validate(test_loader, classifier, args))

    # evaluate on test set
    classifier.load_state_dict(torch.load(logger.get_checkpoint_path('best')))
    acc1 = validate(test_loader, classifier, args)
    print("test acc on target = {}".format(acc1))
    print("oracle acc on target = {}".format(best_test_acc1))
    logger.close()


def train(train_iter: ForeverDataIterator, model: Classifier, optimizer, lr_scheduler: CosineAnnealingLR,
          correlation_alignment_loss: CorrelationAlignmentLoss, num_domains: int, epoch: int, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':4.2f')
    data_time = AverageMeter('Data', ':3.1f')
    losses = AverageMeter('Loss', ':3.2f')
    losses_ce = AverageMeter('CELoss', ':3.2f')
    losses_penalty = AverageMeter('Penalty Loss', ':3.2f')
    cls_accs = AverageMeter('Cls Acc', ':3.1f')

    progress = ProgressMeter(
        args.iters_per_epoch,
        [batch_time, data_time, losses, losses_ce, losses_penalty, cls_accs],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()
    for i in range(args.iters_per_epoch):
        x_all, labels_all = next(train_iter)
        x_all = x_all.to(device)
        labels_all = labels_all.to(device)

        # compute output
        y_all, f_all = model(x_all)

        # measure data loading time
        data_time.update(time.time() - end)

        # separate into different domains
        y_all = y_all.chunk(num_domains, dim=0)
        f_all = f_all.chunk(num_domains, dim=0)
        labels_all = labels_all.chunk(num_domains, dim=0)

        loss_ce = 0
        loss_penalty = 0
        cls_acc = 0
        for domain_i in range(num_domains):
            # cls loss
            y_i, labels_i = y_all[domain_i], labels_all[domain_i]
            loss_ce += F.cross_entropy(y_i, labels_i)
            # update acc
            cls_acc += accuracy(y_i, labels_i)[0] / num_domains
            # correlation alignment loss
            for domain_j in range(domain_i + 1, num_domains):
                f_i = f_all[domain_i]
                f_j = f_all[domain_j]
                loss_penalty += correlation_alignment_loss(f_i, f_j)

        # normalize loss
        loss_ce /= num_domains
        loss_penalty /= num_domains * (num_domains - 1) / 2

        loss = loss_ce + loss_penalty * args.trade_off

        losses.update(loss.item(), x_all.size(0))
        losses_ce.update(loss_ce.item(), x_all.size(0))
        losses_penalty.update(loss_penalty.item(), x_all.size(0))
        cls_accs.update(cls_acc.item(), x_all.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        lr_scheduler.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)


def validate(val_loader: DataLoader, model: Classifier, args: argparse.Namespace) -> float:
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    top5 = AverageMeter('Acc@5', ':6.2f')
    progress = ProgressMeter(
        len(val_loader),
        [batch_time, losses, top1, top5],
        prefix='Test: ')

    # switch to evaluate mode
    model.eval()
    if args.per_class_eval:
        classes = val_loader.dataset.classes
        confmat = ConfusionMatrix(len(classes))
    else:
        confmat = None

    with torch.no_grad():
        end = time.time()
        for i, (images, target) in enumerate(val_loader):
            images = images.to(device)
            target = target.to(device)

            # compute output
            output, _ = model(images)
            loss = F.cross_entropy(output, target)

            # measure accuracy and record loss
            acc1, acc5 = accuracy(output, target, topk=(1, 5))
            if confmat:
                confmat.update(target, output.argmax(1))
            losses.update(loss.item(), images.size(0))
            top1.update(acc1.item(), images.size(0))
            top5.update(acc5.item(), images.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                progress.display(i)

        print(' * Acc@1 {top1.avg:.3f} Acc@5 {top5.avg:.3f}'
              .format(top1=top1, top5=top5))
        if confmat:
            print(confmat.format(classes))

    return top1.avg


if __name__ == '__main__':
    architecture_names = sorted(
        name for name in models.__dict__
        if name.islower() and not name.startswith("__")
        and callable(models.__dict__[name])
    )
    dataset_names = sorted(
        name for name in datasets.__dict__
        if not name.startswith("__") and callable(datasets.__dict__[name])
    )

    parser = argparse.ArgumentParser(description='Baseline for Domain Generalization')
    # dataset parameters
    parser.add_argument('root', metavar='DIR',
                        help='root path of dataset')
    parser.add_argument('-d', '--data', metavar='DATA', default='PACS',
                        help='dataset: ' + ' | '.join(dataset_names) +
                             ' (default: PACS)')
    parser.add_argument('-s', '--sources', nargs='+', default=None,
                        help='source domain(s)')
    parser.add_argument('-t', '--targets', nargs='+', default=None,
                        help='target domain(s)')
    # model parameters
    parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet50',
                        choices=architecture_names,
                        help='backbone architecture: ' +
                             ' | '.join(architecture_names) +
                             ' (default: resnet50)')
    parser.add_argument('--finetune', action='store_true', help='whether use 10x smaller lr for backbone')
    parser.add_argument('--freeze-bn', action='store_true', help='whether freeze all bn layers')
    parser.add_argument('--dropout-p', type=float, default=0.1, help='only activated when freeze-bn is True')
    # training parameters
    parser.add_argument('--trade-off', default=1, type=float,
                        help='the trade off hyper parameter for correlation alignment loss')
    parser.add_argument('-b', '--batch-size', default=36, type=int,
                        metavar='N',
                        help='mini-batch size (default: 36)')
    parser.add_argument('--lr', '--learning-rate', default=5e-4, type=float,
                        metavar='LR', help='initial learning rate', dest='lr')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=0.0005, type=float,
                        metavar='W', help='weight decay (default: 5e-4)')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--epochs', default=20, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-i', '--iters-per-epoch', default=500, type=int,
                        help='Number of iterations per epoch')
    parser.add_argument('-p', '--print-freq', default=100, type=int,
                        metavar='N', help='print frequency (default: 100)')
    parser.add_argument('--seed', default=None, type=int,
                        help='seed for initializing training. ')
    parser.add_argument('--per-class-eval', action='store_true',
                        help='whether output per-class accuracy during evaluation')
    parser.add_argument("--log", type=str, default='src_only',
                        help="Where to save logs, checkpoints and debugging images.")
    parser.add_argument("--phase", type=str, default='train', choices=['train', 'test', 'analysis'],
                        help="When phase is 'test', only test the model."
                             "When phase is 'analysis', only analysis the model.")
    args = parser.parse_args()
    main(args)
