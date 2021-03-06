import math
import sys
import time
import torch
import json

from pycocotools.coco import COCO
import torchvision.models.detection.mask_rcnn
from baseline.coco_utils import get_coco_api_from_dataset
from baseline.coco_eval import CocoEvaluator
import baseline.utils as utils


def train_one_epoch(model, optimizer, data_loader, device, epoch, print_freq):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    header = 'Epoch: [{}]'.format(epoch)

    lr_scheduler = None
    if epoch == 0:
        warmup_factor = 1. / 1000
        warmup_iters = min(1000, len(data_loader) - 1)

        lr_scheduler = utils.warmup_lr_scheduler(optimizer, warmup_iters, warmup_factor)

    for images, targets in metric_logger.log_every(data_loader, print_freq, header):
        images = list(image.to(device) for image in images)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        # DEBUG(rjbruin)
        if len(targets) == 0:
            raise ValueError("There are still samples with zero targets!")

        loss_dict = model(images, targets)
        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())

        loss_value = losses_reduced.item()

        # DEBUG: only catch NaN loss if we don't have anomaly detection enabled
        if not torch.is_anomaly_enabled() and not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        optimizer.step()

        if lr_scheduler is not None:
            lr_scheduler.step()

        metric_logger.update(loss=losses_reduced, **loss_dict_reduced)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])


def _get_iou_types(model):
    model_without_ddp = model
    if isinstance(model, torch.nn.parallel.DistributedDataParallel):
        model_without_ddp = model.module
    iou_types = ["bbox"]
    if isinstance(model_without_ddp, torchvision.models.detection.MaskRCNN):
        iou_types.append("segm")
    if isinstance(model_without_ddp, torchvision.models.detection.KeypointRCNN):
        iou_types.append("keypoints")
    return iou_types


@torch.no_grad()
def evaluate(model, data_loader, device):
    n_threads = torch.get_num_threads()
    # FIXME remove this and make paste_masks_in_image run on the GPU
    torch.set_num_threads(1)
    cpu_device = torch.device("cpu")
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    coco = get_coco_api_from_dataset(data_loader.dataset)
    iou_types = _get_iou_types(model)
    coco_evaluator = CocoEvaluator(coco, iou_types)

    for image, targets in metric_logger.log_every(data_loader, 100, header):
        image = list(img.to(device) for img in image)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        torch.cuda.synchronize()
        model_time = time.time()
        outputs = model(image)

        outputs = [{k: v.to(cpu_device) for k, v in t.items()} for t in outputs]
        model_time = time.time() - model_time

        res = {target["image_id"].item(): output for target, output in zip(targets, outputs)}
        evaluator_time = time.time()
        coco_evaluator.update(res)
        evaluator_time = time.time() - evaluator_time
        metric_logger.update(model_time=model_time, evaluator_time=evaluator_time)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    coco_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    coco_evaluator.accumulate()
    coco_evaluator.summarize()
    torch.set_num_threads(n_threads)
    return coco_evaluator

@torch.no_grad()
def perform_eval_inference(model, data_loader, device, mode):
    # FIXME remove this and make paste_masks_in_image run on the GPU
    torch.set_num_threads(1)
    cpu_device = torch.device("cpu")
    model.eval()
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'

    coco = get_coco_api_from_dataset(data_loader.dataset)
    iou_types = _get_iou_types(model)
    coco_evaluator = CocoEvaluator(coco, iou_types)

    results_for_file = {iou_type: [] for iou_type in coco_evaluator.iou_types}
    for image, targets in metric_logger.log_every(data_loader, 100, header):
        image = list(img.to(device) for img in image)
        
        torch.cuda.synchronize()
        model_time = time.time()
        outputs = model(image)
        model_time = time.time() - model_time

        # DEBUG
        if isinstance(targets, dict):
            targets = [targets]
        targets = [{k: v.to(cpu_device).detach().numpy() for k, v in t.items()} for t in targets]
        outputs = [{k: v.to(cpu_device).detach().numpy() for k, v in o.items()} for o in outputs]

        res = {target["image_id"].item(): output for target, output in zip(targets, outputs)}
        evaluator_time = time.time()
        batch_results_for_file = coco_evaluator.update_inference(res)
        evaluator_time = time.time() - evaluator_time
        metric_logger.update(model_time=model_time, evaluator_time=evaluator_time)
        # coco_evaluator.update(res)
        for iou_type in coco_evaluator.iou_types:
            results_for_file[iou_type].extend(batch_results_for_file[iou_type])

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    # if mode == 'val':
        # coco_evaluator.synchronize_between_processes()
        # # accumulate predictions from all images
        # coco_evaluator.accumulate()
        # coco_evaluator.summarize()
        # torch.set_num_threads(n_threads)
    return results_for_file

@torch.no_grad()
def evaluate_from_results_file(gt_data_loader, results_from_file):
    coco = get_coco_api_from_dataset(gt_data_loader.dataset)
    iou_types = ["bbox"] # NOTE(rjbruin): hardcoded to only do bboxes
    coco_evaluator = CocoEvaluator(coco, iou_types)
    coco_evaluator.put_results(results_from_file)
    coco_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    coco_evaluator.accumulate()
    coco_evaluator.summarize()
    torch.set_num_threads(torch.get_num_threads())
    return coco_evaluator

@torch.no_grad()
def save_groundtruths_coco(gt_data_loader, filepath):
    coco = get_coco_api_from_dataset(gt_data_loader.dataset)
    with open(filepath + '.json', 'w') as f:
        json.dump(coco.dataset, f)