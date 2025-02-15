
# PlacesCNN to predict the scene category, attribute, and class activation map in a single pass
# by Bolei Zhou, sep 2, 2017
# updated, making it compatible to pytorch 1.x in a hacky way

import torch
from torch.autograd import Variable as V
import torchvision.models as models
from torchvision import transforms as trn
from torch.nn import functional as F
import os
import numpy as np
import cv2
from PIL import Image

import argparse
from tqdm import tqdm
import json

 # hacky way to deal with the Pytorch 1.0 update
def recursion_change_bn(module):
    if isinstance(module, torch.nn.BatchNorm2d):
        module.track_running_stats = 1
    else:
        for i, (name, module1) in enumerate(module._modules.items()):
            module1 = recursion_change_bn(module1)
    return module

def load_labels():
    # prepare all the labels
    # scene category relevant
    file_name_category = 'categories_places365_renamed.txt'
    if not os.access(file_name_category, os.W_OK):
        synset_url = 'https://raw.githubusercontent.com/csailvision/places365/master/categories_places365.txt'
        os.system('wget ' + synset_url)
    classes = list()
    with open(file_name_category) as class_file:
        for line in class_file:
            classes.append(line.strip())
    classes = tuple(classes)

    # indoor and outdoor relevant
    file_name_IO = 'IO_places365.txt'
    if not os.access(file_name_IO, os.W_OK):
        synset_url = 'https://raw.githubusercontent.com/csailvision/places365/master/IO_places365.txt'
        os.system('wget ' + synset_url)
    with open(file_name_IO) as f:
        lines = f.readlines()
        labels_IO = []
        for line in lines:
            items = line.rstrip().split()
            labels_IO.append(int(items[-1]) -1) # 0 is indoor, 1 is outdoor
    labels_IO = np.array(labels_IO)

    # scene attribute relevant
    file_name_attribute = 'labels_sunattribute.txt'
    if not os.access(file_name_attribute, os.W_OK):
        synset_url = 'https://raw.githubusercontent.com/csailvision/places365/master/labels_sunattribute.txt'
        os.system('wget ' + synset_url)
    with open(file_name_attribute) as f:
        lines = f.readlines()
        labels_attribute = [item.rstrip() for item in lines]
    file_name_W = 'W_sceneattribute_wideresnet18.npy'
    if not os.access(file_name_W, os.W_OK):
        synset_url = 'http://places2.csail.mit.edu/models_places365/W_sceneattribute_wideresnet18.npy'
        os.system('wget ' + synset_url)
    W_attribute = np.load(file_name_W)

    return classes, labels_IO, labels_attribute, W_attribute

def hook_feature(module, input, output):
    global features_blobs
    features_blobs.append(np.squeeze(output.data.cpu().numpy()))

def returnCAM(feature_conv, weight_softmax, class_idx):
    # generate the class activation maps upsample to 256x256
    size_upsample = (256, 256)
    nc, h, w = feature_conv.shape
    output_cam = []
    for idx in class_idx:
        cam = weight_softmax[class_idx].dot(feature_conv.reshape((nc, h*w)))
        cam = cam.reshape(h, w)
        cam = cam - np.min(cam)
        cam_img = cam / np.max(cam)
        cam_img = np.uint8(255 * cam_img)
        output_cam.append(cv2.resize(cam_img, size_upsample))
    return output_cam

def returnTF():
# load the image transformer
    tf = trn.Compose([
        trn.Resize((224,224)),
        trn.ToTensor(),
        trn.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])
    return tf


def load_model():
    # this model has a last conv feature map as 14x14

    model_file = 'wideresnet18_places365.pth.tar'
    if not os.access(model_file, os.W_OK):
        os.system('wget http://places2.csail.mit.edu/models_places365/' + model_file)
        os.system('wget https://raw.githubusercontent.com/csailvision/places365/master/wideresnet.py')

    import wideresnet
    model = wideresnet.resnet18(num_classes=365)
    checkpoint = torch.load(model_file, map_location=lambda storage, loc: storage)
    state_dict = {str.replace(k,'module.',''): v for k,v in checkpoint['state_dict'].items()}
    model.load_state_dict(state_dict)
    
    # hacky way to deal with the upgraded batchnorm2D and avgpool layers...
    for i, (name, module) in enumerate(model._modules.items()):
        module = recursion_change_bn(model)
    model.avgpool = torch.nn.AvgPool2d(kernel_size=14, stride=1, padding=0)
    
    model.eval()



    # the following is deprecated, everything is migrated to python36

    ## if you encounter the UnicodeDecodeError when use python3 to load the model, add the following line will fix it. Thanks to @soravux
    #from functools import partial
    #import pickle
    #pickle.load = partial(pickle.load, encoding="latin1")
    #pickle.Unpickler = partial(pickle.Unpickler, encoding="latin1")
    #model = torch.load(model_file, map_location=lambda storage, loc: storage, pickle_module=pickle)

    model.eval()
    # hook the feature extractor
    features_names = ['layer4','avgpool'] # this is the last conv layer of the resnet
    for name in features_names:
        model._modules.get(name).register_forward_hook(hook_feature)
    return model

def main(args):

    # load the labels
    classes, labels_IO, labels_attribute, W_attribute = load_labels()

    # load the model
    global features_blobs
    features_blobs = []
    model = load_model()

    # load the transformer
    tf = returnTF() # image transformer

    # get the softmax weight
    params = list(model.parameters())
    weight_softmax = params[-2].data.numpy()
    weight_softmax[weight_softmax<0] = 0
    
    images = {}
    stats = {}
    n = 0

    dirs = os.listdir(args.data_dir)
    for dir in tqdm(dirs):
        files_dir = os.path.join(args.data_dir, dir)

        if (not os.path.isdir(files_dir)):
            continue

        files = os.listdir(files_dir)
        files = [file for file in files if "_mask" not in file]

        for file in tqdm(files):

            # load the test image
            filename = os.path.join(args.data_dir, dir, file)

            if (args.use_masks):

                filename_mask = os.path.join(args.data_dir, dir, file.replace(".jpg", "_mask.jpg"))

                if (not os.path.exists(filename_mask)):
                    continue

                img = cv2.imread(filename).astype('float32')
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                mask = cv2.imread(filename_mask).astype('float32')/255
                img = img*mask

                #img = cv2.imread(filename)
                #img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                #mask = cv2.imread(filename_mask)
                #img = cv2.inpaint(img,255-mask[:,:,1],3,cv2.INPAINT_TELEA)

                img = Image.fromarray(img.astype('uint8'))
            else:
                img = Image.open(filename)

            input_img = V(tf(img).unsqueeze(0))
            
            ann = {}

            # forward pass
            logit = model.forward(input_img)
            h_x = F.softmax(logit, 1).data.squeeze()
            probs, idx = h_x.sort(0, True)
            probs = probs.numpy()
            idx = idx.numpy()

            #print('RESULT ON ' + img_name)

            # output the IO prediction
            io_image = np.mean(labels_IO[idx[:10]]) # vote for the indoor or outdoor
            ann["outdoor"] = io_image

            # output the prediction of scene category
            scene_categories = []
            for i in range(0, 5):
                cls = classes[idx[i]]
                prob = probs[i]

                if (prob < .05):
                    continue

                scene_categories.append((cls, str(prob)))

                if (cls in stats):
                    stats[cls] += prob
                else:
                    stats[cls] = prob

            ann["scene_categories"] = scene_categories

            # output the scene attributes
            scene_attributes = []
            responses_attribute = W_attribute.dot(features_blobs[1])
            idx_a = np.argsort(responses_attribute)
            scene_attributes = [labels_attribute[idx_a[i]] for i in range(-1,-10,-1)]

            ann["scene_attributes"] = scene_attributes

            images[os.path.join(dir, file)] = ann

            n +=1

            #if (n > 100):
            #    break

    sum_prob = 0
    for cls in stats.keys():
        sum_prob += stats[cls]

    for cls in stats.keys():
        stats[cls] /= sum_prob

    stats_list = []
    for cls in stats.keys():
        prob = stats[cls]
        if (prob < .05):
            continue

        stats_list.append((prob, cls))

    stats_list.sort(reverse=True)

    for cls in stats.keys():
        stats[cls] = str(stats[cls])

    images["stats"] = stats

    print(stats_list)

    output_file_path = os.path.join(args.data_dir, "scene_annotations.json")
    with open(output_file_path, 'w+') as json_file:
        json_file.write(json.dumps(images))

parser = argparse.ArgumentParser(description='Places 365 inference')
#parser.add_argument('--data_dir', metavar='DIR', default="E:/Research/Images/FineGrained/fgvc-aircraft-2013b/test", help='path to dataset')
parser.add_argument('data_dir', metavar='DIR', help='path to dataset')
parser.add_argument('--use_masks', action="store_true")

if __name__ == "__main__":
    # Parses command line and config file arguments.
    args = parser.parse_args()
    main(args)
