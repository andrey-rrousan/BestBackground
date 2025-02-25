import copy
import io
import os
from urllib.parse import urlencode

import numpy as np
import requests
import torch
import torchvision
import torchvision.transforms.functional as TF
from PIL import Image, ImageFilter
from torchvision import transforms
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

MODEL_DETECTION_NAME = 'model_jew_detect_01.05.2023.md'
MODEL_MASK_NAME = 'model_jew_mask_02.05.2023.md'

BASE_URL_YANDEX_CLOUD = 'https://cloud-api.yandex.net/v1/disk/public/resources/download?'
URL_DETECTION_MODEL = 'https://disk.yandex.ru/d/ZIFMEHuc2xoAOw'
URL_SEGMENTATION_MODEL = 'https://disk.yandex.ru/d/hMja6N8LUz4R3w'

STEPS_FOR_THRESHOLD = [0.8, 0.7, 0.6, 0.5]
GAUSSIAN_BLUR_VALUE = 20


def PIL_image_to_tensor(image: Image, model_shape):
    '''
    Преобразует изображение PIL Image в нормализованное и изменяет размеры на model_shape

    :param image: изображение PIL Image
    :param model_shape: новые размеры изображения
    :return: PIL Image нормализованное размера model_shape
    '''
    test_transform = torch.nn.Sequential(
        transforms.Resize(model_shape),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    )

    image = TF.to_tensor(image)
    image = test_transform(image)
    return image


def PIL_images_to_tensors(images, model_shape):
    '''
    Преобразует image формата PIL_image в нормализованный тензор размера model_shape

    :param images: изображение PIL_image
    :param model_shape: новые размеры изображения
    :return: тензор размером model_shape
    '''
    images = [i.convert('RGB') for i in images]
    return [PIL_image_to_tensor(i, model_shape=model_shape) for i in images]


def load_model_detection(name):
    '''
    Загрузка модели детектции

    :param name: путь к файлу модели детектции
    :return: модель
    '''
    model = torchvision.models.detection.fasterrcnn_resnet50_fpn(pretrained=True)

    num_classes = 2  # 1 class (wheat) + background
    # get number of input features for the classifier
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    # replace the pre-trained head with a new one
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)

    if torch.cuda.is_available():
        model.load_state_dict(torch.load(name))
    else:
        model.load_state_dict(torch.load(name, map_location=torch.device('cpu')))

    model.to(DEVICE)
    return model


def load_model_mask(name):
    '''
    Загрузка модели сегментации

    :param name: путь к файлу модели сегментации
    :return: модель
    '''
    model = torchvision.models.detection.maskrcnn_resnet50_fpn(pretrained=True)

    in_features = model.roi_heads.box_predictor.cls_score.in_features
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes=2)

    if torch.cuda.is_available():
        model.load_state_dict(torch.load(name))
    else:
        model.load_state_dict(torch.load(name, map_location=torch.device('cpu')))

    params = [p for p in model.parameters() if p.requires_grad]

    model.to(DEVICE)

    return model


def jewellery_detection_get_rx_ry(images, model, model_shape=(384, 384), k=0):
    '''
    Делает детекцию объектов. Выдает относительные координаты

    :param images: список PIL Image изображения - желательно делать маленькими батчами
    :param model: model for detection
    :param model_shape: shape of images in model
    :return" relative coordinates (i.e. coordinate devide dimenstion of image)
            of detection box in a format (x1, y1, x2, y1)
    '''
    model.eval()

    aug_image = PIL_images_to_tensors(images, model_shape=model_shape)
    aug_image = list(image.to(DEVICE) for image in aug_image)
    model.to(DEVICE)
    predictions = model(aug_image)

    result = []

    for i, prediction in enumerate(predictions):
        pred_boxs = prediction['boxes']
        pred_boxs_numpy = pred_boxs.detach().cpu().numpy()

        pred_scores = prediction['scores']
        pred_score = pred_scores[0].item()

        pred_scores_numpy = pred_scores.detach().cpu().numpy()

        # вывод изображения с наилучшими несколькими detection box
        for threshold in STEPS_FOR_THRESHOLD:
            list_ind = np.where(pred_scores_numpy > max(STEPS_FOR_THRESHOLD))[0]
            ind_max = len(list_ind)
            if ind_max > 0:
                break
        if ind_max == 0:
            ind_max = 1

        # объединение нескольких detection box в один
        pred_boxs_good = pred_boxs_numpy[:ind_max]

        best_x1 = min([p[0] for p in pred_boxs_good])
        best_y1 = min([p[1] for p in pred_boxs_good])
        best_x2 = max([p[2] for p in pred_boxs_good])
        best_y2 = max([p[3] for p in pred_boxs_good])

        rx1 = best_x1 / model_shape[0]
        ry1 = best_y1 / model_shape[1]
        rx2 = best_x2 / model_shape[0]
        ry2 = best_y2 / model_shape[1]

        if k > 0:
            rx1 = max(rx1 - k, 0)
            rx2 = min(rx2 + k, 1)
            ry1 = max(ry1 - k, 0)
            ry2 = min(ry2 + k, 1)

        result.append(((rx1, ry1, rx2, ry2), pred_score))

    return result


def crop_image(image, rbox):
    '''
    Обрезка изображения

    :param image: изображение
    :param rbox: список относительных координат
    :return: обрезанное изображение
    '''
    image_shape = image.size
    x1 = image_shape[0] * rbox[0]
    y1 = image_shape[1] * rbox[1]
    x2 = image_shape[0] * rbox[2]
    y2 = image_shape[1] * rbox[3]
    return image.crop((x1, y1, x2, y2))


def jewellery_mask(images, model, model_shape=(384, 384)):
    '''
    Инференс модели по обределению маски

    :param images: исходные изображения формата PIL Image
    :param model: модель
    :param model_shape: размер изобржанения на входе в модель
    :return: маска
    '''
    transform = transforms.ToPILImage()

    model.eval()

    aug_image = PIL_images_to_tensors(images, model_shape=model_shape)
    aug_image = list(image.to(DEVICE) for image in aug_image)
    model.to(DEVICE)
    predictions = model(aug_image)

    result = []

    try:
        for i, prediction in enumerate(predictions):
            pred_score = prediction['scores'][0].item()
            pred_mask = transform(prediction['masks'][0])
            result.append((pred_mask, pred_score))
    except:
        pred_score = None
        pred_mask = None
        result.append((pred_mask, pred_score))

    return result


def clean_image_with_mask(image, r=0.6, min_blur=0.1):
    '''
    Обработка маски. Сначала к 1 приравнивается все значения больше r, остальные к 0.
    Оставшиеся к нулю приравниваются.
    После этого происходит размытие маски

    :param image: исходное изображение формата PIL Image
    :param r: порог для округения
    :param min_blur: параметр размытия
    :return: PIL Image
    '''
    if r == None:
        return image

    image = np.array(image)

    x = image[:, :, 3] / 255.0 + (r - 0.5)
    x = np.clip(x, min_blur, 1)
    image[:, :, 3] = x * 255

    return Image.fromarray(image)


def jewellery_detect_crop_mask(images, model_detection, model_mask, model_shape=(384, 384), k=0.02):
    '''
    Совместная работа двух моделей. Формирование словаря резултатов на выходе

    :param images: исходные изображения формата PIL Image
    :param model_detection: модель детектции
    :param model_mask: модель сегментации
    :param model_shape: размер изобржанения на входе в модели
    :param k: расширение рамки для детектции на 100*k процентов
    :return: словарь с результатами работы модели
    '''
    rboxes = jewellery_detection_get_rx_ry(images, model_detection, model_shape=model_shape, k=k)

    cropped_images = []
    detect_acc = []

    rows = len(rboxes)
    for k in range(rows):
        rbox, acc = rboxes[k]
        cropped_image = crop_image(images[k], rbox)
        cropped_images.append(cropped_image)
        detect_acc.append(acc)

    masks = jewellery_mask(cropped_images, model_mask)

    result = []
    for k in range(rows):
        res = {}
        res['cropped_image'] = cropped_images[k]
        res['detection_accurancy'] = detect_acc[k]
        res['mask'] = masks[k][0]
        res['segmentation_accurancy'] = masks[k][1]

        result.append(res)

    return result


def get_jewellery_image_(images_original, model_detection, model_mask,
                         model_shape=(384, 384),
                         k=0.05,
                         threshold_detect=0.98,
                         threshold_segmentation=0.99,
                         threshold_clean_mask=0.9,
                         show_bad_results=True,
                         min_blur=0.1,
                         gaussian_blur=20,
                         ):
    '''
    Инференс моделе с настройками

    :param images_original: исходные изображения в формате PIL Image
    :param model_detection: модель детектции
    :param model_mask: модель сегментации
    :param model_shape: размер изобржанения на входе в модели
    :param k: расширение рамки для детектции на 100*k процентов
    :param threshold_detect: порог уверенности модели детектции
    :param threshold_segmentation: порог уверенности модели сегментации
    :param threshold_clean_mask: порог для сегментации, свыше которого маска равна 1
    :param show_bad_results: показывать ли плохие результаты
    :param min_blur: параметры размытия
    :param gaussian_blur: параметры размытия
    :return: словарь с результатами работы модели
    '''
    predict = jewellery_detect_crop_mask(images_original, model_detection, model_mask, model_shape=model_shape, k=k)

    rows = len(images_original)

    for k in range(rows):
        if predict[k]['mask'] is not None:

            mask = predict[k]['mask']

            q = copy.copy(predict[k]['cropped_image'])
            q.putalpha(mask.resize(predict[k]['cropped_image'].size))
            q_clean = clean_image_with_mask(q, r=threshold_clean_mask, min_blur=min_blur)

            rgb = q_clean.convert('RGB')
            blurred = rgb.filter(ImageFilter.GaussianBlur(gaussian_blur))
            q_clean = Image.composite(rgb, blurred, mask.resize(predict[k]['cropped_image'].size))

            predict[k]['image_segmented'] = q_clean

            if predict[k]['detection_accurancy'] < threshold_detect and not show_bad_results:
                predict[k]['cropped_image'] = None

            if predict[k]['segmentation_accurancy'] < threshold_segmentation and not show_bad_results:
                predict[k]['image_segmented'] = None

            predict[k]['image_original'] = images_original[k]

    return predict


def get_jewellery_image(images_original, model_detection, model_mask,
                        model_shape=(384, 384),
                        k=0.05,
                        threshold_detect=0.98,
                        threshold_segmentation=0.99,
                        threshold_clean_mask=0.9,
                        show_bad_results=True,
                        path=None,
                        min_blur=0.1,
                        gaussian_blur=20,
                        ):
    '''

    :param images_original: исходные изображения в формате PIL Image
    :param model_detection: модель детектции
    :param model_mask: модель сегментации
    :param model_shape: размер изобржанения на входе в модели
    :param k: расширение рамки для детектции на 100*k процентов
    :param threshold_detect: порог уверенности модели детектции
    :param threshold_segmentation: порог уверенности модели сегментации
    :param threshold_clean_mask: порог для сегментации, свыше которого маска равна 1
    :param show_bad_results: показывать ли плохие результаты
    :param min_blur: параметры размытия
    :param path: путь для сохранения результатов. Если None, то нет сохранений
    :param min_blur: параметры размытия
    :param gaussian_blur: параметры размытия
    :return: словарь с результатами работы модели
    '''
    if not isinstance(images_original, list):
        images_original = [images_original]

    if isinstance(images_original[0], str):
        try:
            images_original = [Image.open(i) for i in images_original]
        except:
            print('Loading file error')

    if not isinstance(images_original[0], Image.Image):
        try:
            images_original = [Image.open(io.BytesIO(i)) for i in images_original]
        except:
            print('Loading file error')

    predict = get_jewellery_image_(images_original, model_detection, model_mask,
                                   model_shape=model_shape,
                                   k=k,
                                   threshold_detect=threshold_detect,
                                   threshold_segmentation=threshold_segmentation,
                                   threshold_clean_mask=threshold_clean_mask,
                                   show_bad_results=show_bad_results,
                                   min_blur=min_blur,
                                   gaussian_blur=gaussian_blur
                                   )

    if path is not None:
        for i, image in enumerate(predict):
            try:
                if image['cropped_image'] is not None:
                    file_path = os.path.join(path, f"cropped_image_{i + 1}.png")
                    image['cropped_image'].save(file_path)
                if image['image_segmented'] is not None:
                    file_path = os.path.join(path, f"image_segmented_{i + 1}.png")
                    image['image_segmented'].save(file_path)
            except:
                print('Saving files error')

    return predict


def init_models(model_detection_name=MODEL_DETECTION_NAME,
                model_mask_name=MODEL_MASK_NAME,
                use_gpu=True):
    '''
    Подгружаем модели с яндекс облака, если они еще не загружены
    После этого модели подгружаются в память

    :param model_detection: модель детектции
    :param model_mask: модель сегментации
    :param use_gpu: использовать ли GPU, иначе используется CPU
    :return: модель детектции, модель сегментации
    '''
    global DEVICE
    if use_gpu:
        DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        if DEVICE == "cpu":
            print('Could not enable CUDA, using CPU instead.')
    else:
        DEVICE = "cpu"
    models_folder = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'models')

    if not os.path.exists(models_folder):
        print('creating folder /models')
        os.makedirs(models_folder)

    if not os.path.isfile(os.path.join(models_folder, model_detection_name)):
        print('downloading model detection model')

        base_url = BASE_URL_YANDEX_CLOUD
        public_key = URL_DETECTION_MODEL

        # Получаем загрузочную ссылку
        final_url = base_url + urlencode(dict(public_key=public_key))
        response = requests.get(final_url)
        download_url = response.json()['href']

        # Загружаем файл и сохраняем его
        download_response = requests.get(download_url)

        destination = os.path.join(models_folder, model_detection_name)

        with open(destination, 'wb') as f:  # Здесь укажите нужный путь к файлу
            f.write(download_response.content)

    if not os.path.isfile(os.path.join(models_folder, model_mask_name)):
        print('downloading model segmentation model')

        base_url = BASE_URL_YANDEX_CLOUD
        public_key = URL_SEGMENTATION_MODEL

        # Получаем загрузочную ссылку
        final_url = base_url + urlencode(dict(public_key=public_key))
        response = requests.get(final_url)
        download_url = response.json()['href']

        # Загружаем файл и сохраняем его
        download_response = requests.get(download_url)

        destination = os.path.join(models_folder, model_mask_name)

        with open(destination, 'wb') as f:
            f.write(download_response.content)

    model_detection = load_model_detection(os.path.join(models_folder, model_detection_name))
    model_mask = load_model_mask(os.path.join(models_folder, model_mask_name))

    model_detection.eval()
    model_mask.eval()

    return model_detection, model_mask


if __name__ == '__main__':
    test_img_folder = os.path.abspath(os.path.dirname(__file__))
    images = os.path.join(test_img_folder, 'test.jpg')

    '''
    Инициализация, то есть подгрузка моделей проходит через функцию init_models
    Если папка models пустая, то загружает модели с моего гугл диска автоматически
    Возвращает model_detection, model_mas
    '''
    model_detection, model_mask = init_models()

    '''
    Обработка изображений проходит в следующей функции.
    Можно подавать на вход одно из следуюшего:
    - ссылка на изображение
    - PIL image
    - бинарный файл
    - список ссылок на изображения
    - список PIL image
    - список бинарных файлов

    Выдает результаты в виде списка словарей. Там картинки в PIL Image. Если задать path, то в path сохранит картинки-результаты
    '''
    result = get_jewellery_image(images, model_detection, model_mask, path='', gaussian_blur=GAUSSIAN_BLUR_VALUE)
    print(result)
