# Collaborative Uncertainty in Multi-Agent Multi-Modal Trajectory Forecasting

## Overview

This code is based on the official code of LaneGCN ([Paper](https://arxiv.org/pdf/2007.13732.pdf); [Github](https://github.com/uber-research/LaneGCN)).

A quick summary of different folders:

- `cu.py` contains the source code for the model with proposed laplacian collaborative uncertainty framework.

- `iu.py` contains the source code for the model with laplacian individual uncertainty framework.

Please kindly find the description about the rest of code in [LaneGCN](https://github.com/uber-research/LaneGCN)).

## Examples

You can download the pretrained models [here](https://drive.google.com/file/d/1uU4JhoUl7FZvQwuvRIwQkykHMeRIpc1O/view?usp=sharing).

- Training the model, run: horovodrun -np 2 -H localhost:4 python train.py -m "model_name" (e.g. lanegcn_cu).

- Validating the model, run: horovodrun -np 2 -H localhost:4 python train.py -m "model_name" (e.g. lanegcn_cu) --resume "path of weights" (e.g. ../cu_m.ckpt) --eval

- Testing the model, run: python test.py -m "model_name" (e.g. lanegcn_cu) --weight= "path of weights" (e.g. ../cu_m.ckpt) --split=test
