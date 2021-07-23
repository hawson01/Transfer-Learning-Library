from .mixstyle import MixStyle
from common.vision.models.reid.resnet import ReidResNet
from common.vision.models.resnet import ResNet, load_state_dict_from_url, model_urls, BasicBlock, Bottleneck

__all__ = ['resnet18', 'resnet34', 'resnet50', 'resnet101']


def _resnet_with_mix_style(arch, block, layers, pretrained, progress, mix_layers=None, mix_p=0.5, mix_alpha=0.1,
                           resnet_class=ResNet, **kwargs):
    r"""Construct resnet with MixStyle modules

    Args:
        arch (str): resnet architecture (eg resnet50)
        block (class): class of resnet block
        layers (list): depth list of each block
        pretrained (bool): if True, load imagenet pre-trained model parameters
        progress (bool): whether or not to display a progress bar to stderr
        mix_layers (list): layers to insert MixStyle module after, valid choices: ['layer1', 'layer2', 'layer3']
        mix_p (float): probability to apply MixStyle during forward pass
        mix_alpha (float): parameter alpha for beta distribution
        resnet_class (class): base resnet class to inherit from, for image classification task, this can be 'ResNet'
                            for reid task, this can be 'ReidResNet'
    """

    if mix_layers is None:
        mix_layers = []

    available_resnet_class = [ResNet, ReidResNet]
    assert resnet_class in available_resnet_class

    class ResNetWithMixStyleModule(resnet_class):
        def __init__(self, mix_layers, mix_p=0.5, mix_alpha=0.1, *args, **kwargs):
            super(ResNetWithMixStyleModule, self).__init__(*args, **kwargs)
            self.mixStyleModule = MixStyle(p=mix_p, alpha=mix_alpha)
            for layer in mix_layers:
                assert layer in ['layer1', 'layer2', 'layer3']
            self.apply_layers = mix_layers

        def forward(self, x):
            x = self.conv1(x)
            x = self.bn1(x)
            # turn on relu activation here **except for** reid tasks
            if resnet_class != ReidResNet:
                x = self.relu(x)
            x = self.maxpool(x)

            x = self.layer1(x)
            if 'layer1' in self.apply_layers:
                x = self.mixStyleModule(x)
            x = self.layer2(x)
            if 'layer2' in self.apply_layers:
                x = self.mixStyleModule(x)
            x = self.layer3(x)
            if 'layer3' in self.apply_layers:
                x = self.mixStyleModule(x)
            x = self.layer4(x)

            return x

    model = ResNetWithMixStyleModule(mix_layers=mix_layers, mix_p=mix_p, mix_alpha=mix_alpha, block=block,
                                     layers=layers, **kwargs)
    if pretrained:
        model_dict = model.state_dict()
        pretrained_dict = load_state_dict_from_url(model_urls[arch],
                                                   progress=progress)
        # remove keys from pretrained dict that doesn't appear in model dict
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        model.load_state_dict(pretrained_dict, strict=False)
    return model


def resnet18(pretrained=False, progress=True, **kwargs):
    r"""ResNet-18 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet_with_mix_style('resnet18', BasicBlock, [2, 2, 2, 2], pretrained, progress,
                                  **kwargs)


def resnet34(pretrained=False, progress=True, **kwargs):
    r"""ResNet-34 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet_with_mix_style('resnet34', BasicBlock, [3, 4, 6, 3], pretrained, progress,
                                  **kwargs)


def resnet50(pretrained=False, progress=True, **kwargs):
    r"""ResNet-50 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet_with_mix_style('resnet50', Bottleneck, [3, 4, 6, 3], pretrained, progress,
                                  **kwargs)


def resnet101(pretrained=False, progress=True, **kwargs):
    r"""ResNet-101 model from
    `"Deep Residual Learning for Image Recognition" <https://arxiv.org/pdf/1512.03385.pdf>`_

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
        progress (bool): If True, displays a progress bar of the download to stderr
    """
    return _resnet_with_mix_style('resnet101', Bottleneck, [3, 4, 23, 3], pretrained, progress,
                                  **kwargs)
