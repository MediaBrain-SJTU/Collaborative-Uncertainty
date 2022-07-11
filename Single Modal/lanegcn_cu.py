# Copyright (c) 2020 Uber Technologies, Inc.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import os
import sys
from fractions import gcd
from numbers import Number

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from data import ArgoDataset, collate_fn
from utils import gpu, to_long,  Optimizer, StepLR

from layers import Conv1d, Res1d, Linear, LinearRes, Null
from numpy import float64, ndarray
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union


file_path = os.path.abspath(__file__)
root_path = os.path.dirname(file_path)
data_path = './'
model_name = os.path.basename(file_path).split(".")[0]

### config ###
config = dict()
"""Train"""
config["display_iters"] = 205942
config["val_iters"] = 205942 * 1
config["save_freq"] = 1.0
config["epoch"] = 0
config["horovod"] = True
config["opt"] = "adam"
config["num_epochs"] = 60
config["lr"] = [1e-3, 1e-4, 1e-5]
config["lr_epochs"] = [17,20]
config["lr_func"] = StepLR(config["lr"], config["lr_epochs"])


if "save_dir" not in config:
    config["save_dir"] = os.path.join(
        root_path, "results", model_name
    )

if not os.path.isabs(config["save_dir"]):
    config["save_dir"] = os.path.join(root_path, "results", config["save_dir"])

config["batch_size"] = 32
config["val_batch_size"] = 32
config["workers"] = 0
config["val_workers"] = config["workers"]


"""Dataset"""
# Raw Dataset
config["train_split"] = os.path.join(
    data_path, "dataset/train/data"
)
config["val_split"] = os.path.join(data_path, "dataset/val/data")
config["test_split"] = os.path.join("./test_obs/data")

# Preprocessed Dataset
config["preprocess"] = True # whether use preprocess or not
config["preprocess_train"] = os.path.join(
    "./lanegcn", "train_crs_dist6_angle90.p"
)
config["preprocess_val"] = os.path.join(
    "./lanegcn", "val_crs_dist6_angle90.p"
)
config['preprocess_test'] = os.path.join("./lanegcn", 'test_test.p')

"""Model"""
config["rot_aug"] = False
config["pred_range"] = [-100.0, 100.0, -100.0, 100.0]
config["num_scales"] = 6
config["n_actor"] = 128
config["f_d"] = 128
config["n_map"] = 128
config["f_weight"] = 12.0
config["actor2map_dist"] = 7.0
config["map2actor_dist"] = 6.0
config["actor2actor_dist"] = 100.0
config["pred_size"] = 30
config["pred_step"] = 1
config["num_preds"] = config["pred_size"] // config["pred_step"]
config["num_mods"] = 1
config["cls_coef"] = 0.0
config["reg_coef"] = 1.0
config["mgn"] = 0.2
config["cls_th"] = 2.0
config["cls_ignore"] = 0.2
config["max_an"] = 0.0
### end of config ###

class Net(nn.Module):
    """
    Lane Graph Network contains following components:
        1. ActorNet: a 1D CNN to process the trajectory input
        2. MapNet: LaneGraphCNN to learn structured map representations 
           from vectorized map data
        3. Actor-Map Fusion Cycle: fuse the information between actor nodes 
           and lane nodes:
            a. A2M: introduces real-time traffic information to 
                lane nodes, such as blockage or usage of the lanes
            b. M2M:  updates lane node features by propagating the 
                traffic information over lane graphs
            c. M2A: fuses updated map features with real-time traffic 
                information back to actors
            d. A2A: handles the interaction between actors and produces
                the output actor features
        4. PredNet: prediction header for motion forecasting using 
           feature from A2A
    """
    def __init__(self, config):
        super(Net, self).__init__()
        self.config = config

        self.actor_net = ActorNet(config)
        self.map_net = MapNet(config)

        self.a2m = A2M(config)
        self.m2m = M2M(config)
        self.m2a = M2A(config)
        self.a2a = A2A(config)

        self.pred_net = PredNet(config)

    def forward(self, data: Dict) -> Dict[str, List[Tensor]]:
        # construct actor feature
        actors, actor_idcs = actor_gather(gpu(data["feats"]))
        actor_ctrs = gpu(data["ctrs"])
        actors = self.actor_net(actors)

        # construct map features
        graph = graph_gather(to_long(gpu(data["graph"])))
        nodes, node_idcs, node_ctrs = self.map_net(graph)

        # actor-map fusion cycle 
        nodes = self.a2m(nodes, graph, actors, actor_idcs, actor_ctrs)
        nodes = self.m2m(nodes, graph)
        actors = self.m2a(actors, actor_idcs, actor_ctrs, nodes, node_idcs, node_ctrs)
        actors = self.a2a(actors, actor_idcs, actor_ctrs)

        # prediction
        out = self.pred_net(actors, actor_idcs, actor_ctrs)
        rot, orig = gpu(data["rot"]), gpu(data["orig"])
        # transform prediction to world coordinates
        for i in range(len(out["reg"])):
            out["reg"][i] = torch.matmul(out["reg"][i], rot[i]) + orig[i].view(
                1, 1, 1, -1
            )
        return out



def actor_gather(actors: List[Tensor]) -> Tuple[Tensor, List[Tensor]]:
    batch_size = len(actors)
    num_actors = [len(x) for x in actors]

    actors = [x.transpose(1, 2) for x in actors]
    actors = torch.cat(actors, 0)

    actor_idcs = []
    count = 0
    for i in range(batch_size):
        idcs = torch.arange(count, count + num_actors[i]).to(actors.device)
        actor_idcs.append(idcs)
        count += num_actors[i]
    return actors, actor_idcs


def graph_gather(graphs):
    batch_size = len(graphs)
    node_idcs = []
    count = 0
    counts = []
    for i in range(batch_size):
        counts.append(count)
        idcs = torch.arange(count, count + graphs[i]["num_nodes"]).to(
            graphs[i]["feats"].device
        )
        node_idcs.append(idcs)
        count = count + graphs[i]["num_nodes"]

    graph = dict()
    graph["idcs"] = node_idcs
    graph["ctrs"] = [x["ctrs"] for x in graphs]

    for key in ["feats", "turn", "control", "intersect"]:
        graph[key] = torch.cat([x[key] for x in graphs], 0)

    for k1 in ["pre", "suc"]:
        graph[k1] = []
        for i in range(len(graphs[0]["pre"])):
            graph[k1].append(dict())
            for k2 in ["u", "v"]:
                graph[k1][i][k2] = torch.cat(
                    [graphs[j][k1][i][k2] + counts[j] for j in range(batch_size)], 0
                )

    for k1 in ["left", "right"]:
        graph[k1] = dict()
        for k2 in ["u", "v"]:
            temp = [graphs[i][k1][k2] + counts[i] for i in range(batch_size)]
            temp = [
                x if x.dim() > 0 else graph["pre"][0]["u"].new().resize_(0)
                for x in temp
            ]
            graph[k1][k2] = torch.cat(temp)
    return graph


class ActorNet(nn.Module):
    """
    Actor feature extractor with Conv1D
    """
    def __init__(self, config):
        super(ActorNet, self).__init__()
        self.config = config
        norm = "GN"
        ng = 1

        n_in = 3
        n_out = [32, 64, 128]
        blocks = [Res1d, Res1d, Res1d]
        num_blocks = [2, 2, 2]

        groups = []
        for i in range(len(num_blocks)):
            group = []
            if i == 0:
                group.append(blocks[i](n_in, n_out[i], norm=norm, ng=ng))
            else:
                group.append(blocks[i](n_in, n_out[i], stride=2, norm=norm, ng=ng))

            for j in range(1, num_blocks[i]):
                group.append(blocks[i](n_out[i], n_out[i], norm=norm, ng=ng))
            groups.append(nn.Sequential(*group))
            n_in = n_out[i]
        self.groups = nn.ModuleList(groups)

        n = config["n_actor"]
        lateral = []
        for i in range(len(n_out)):
            lateral.append(Conv1d(n_out[i], n, norm=norm, ng=ng, act=False))
        self.lateral = nn.ModuleList(lateral)

        self.output = Res1d(n, n, norm=norm, ng=ng)

    def forward(self, actors: Tensor) -> Tensor:
        out = actors

        outputs = []
        for i in range(len(self.groups)):
            out = self.groups[i](out)
            outputs.append(out)

        out = self.lateral[-1](outputs[-1])
        for i in range(len(outputs) - 2, -1, -1):
            out = F.interpolate(out, scale_factor=2, mode="linear", align_corners=False)
            out += self.lateral[i](outputs[i])

        out = self.output(out)[:, :, -1]
        return out


class MapNet(nn.Module):
    """
    Map Graph feature extractor with LaneGraphCNN
    """
    def __init__(self, config):
        super(MapNet, self).__init__()
        self.config = config
        n_map = config["n_map"]
        norm = "GN"
        ng = 1

        self.input = nn.Sequential(
            nn.Linear(2, n_map),
            nn.ReLU(inplace=True),
            Linear(n_map, n_map, norm=norm, ng=ng, act=False),
        )
        self.seg = nn.Sequential(
            nn.Linear(2, n_map),
            nn.ReLU(inplace=True),
            Linear(n_map, n_map, norm=norm, ng=ng, act=False),
        )

        keys = ["ctr", "norm", "ctr2", "left", "right"]
        for i in range(config["num_scales"]):
            keys.append("pre" + str(i))
            keys.append("suc" + str(i))

        fuse = dict()
        for key in keys:
            fuse[key] = []

        for i in range(4):
            for key in fuse:
                if key in ["norm"]:
                    fuse[key].append(nn.GroupNorm(gcd(ng, n_map), n_map))
                elif key in ["ctr2"]:
                    fuse[key].append(Linear(n_map, n_map, norm=norm, ng=ng, act=False))
                else:
                    fuse[key].append(nn.Linear(n_map, n_map, bias=False))

        for key in fuse:
            fuse[key] = nn.ModuleList(fuse[key])
        self.fuse = nn.ModuleDict(fuse)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, graph):
        if (
            len(graph["feats"]) == 0
            or len(graph["pre"][-1]["u"]) == 0
            or len(graph["suc"][-1]["u"]) == 0
        ):
            temp = graph["feats"]
            return (
                temp.new().resize_(0),
                [temp.new().long().resize_(0) for x in graph["node_idcs"]],
                temp.new().resize_(0),
            )

        ctrs = torch.cat(graph["ctrs"], 0)
        feat = self.input(ctrs)
        feat += self.seg(graph["feats"])
        feat = self.relu(feat)

        """fuse map"""
        res = feat
        for i in range(len(self.fuse["ctr"])):
            temp = self.fuse["ctr"][i](feat)
            for key in self.fuse:
                if key.startswith("pre") or key.startswith("suc"):
                    k1 = key[:3]
                    k2 = int(key[3:])
                    temp.index_add_(
                        0,
                        graph[k1][k2]["u"],
                        self.fuse[key][i](feat[graph[k1][k2]["v"]]),
                    )

            if len(graph["left"]["u"] > 0):
                temp.index_add_(
                    0,
                    graph["left"]["u"],
                    self.fuse["left"][i](feat[graph["left"]["v"]]),
                )
            if len(graph["right"]["u"] > 0):
                temp.index_add_(
                    0,
                    graph["right"]["u"],
                    self.fuse["right"][i](feat[graph["right"]["v"]]),
                )

            feat = self.fuse["norm"][i](temp)
            feat = self.relu(feat)

            feat = self.fuse["ctr2"][i](feat)
            feat += res
            feat = self.relu(feat)
            res = feat
        return feat, graph["idcs"], graph["ctrs"]


class A2M(nn.Module):
    """
    Actor to Map Fusion:  fuses real-time traffic information from
    actor nodes to lane nodes
    """
    def __init__(self, config):
        super(A2M, self).__init__()
        self.config = config
        n_map = config["n_map"]
        norm = "GN"
        ng = 1

        """fuse meta, static, dyn"""
        self.meta = Linear(n_map + 4, n_map, norm=norm, ng=ng)
        att = []
        for i in range(2):
            att.append(Att(n_map, config["n_actor"]))
        self.att = nn.ModuleList(att)

    def forward(self, feat: Tensor, graph: Dict[str, Union[List[Tensor], Tensor, List[Dict[str, Tensor]], Dict[str, Tensor]]], actors: Tensor, actor_idcs: List[Tensor], actor_ctrs: List[Tensor]) -> Tensor:
        """meta, static and dyn fuse using attention"""
        meta = torch.cat(
            (
                graph["turn"],
                graph["control"].unsqueeze(1),
                graph["intersect"].unsqueeze(1),
            ),
            1,
        )
        feat = self.meta(torch.cat((feat, meta), 1))

        for i in range(len(self.att)):
            feat = self.att[i](
                feat,
                graph["idcs"],
                graph["ctrs"],
                actors,
                actor_idcs,
                actor_ctrs,
                self.config["actor2map_dist"],
            )
        return feat


class M2M(nn.Module):
    """
    The lane to lane block: propagates information over lane
            graphs and updates the features of lane nodes
    """
    def __init__(self, config):
        super(M2M, self).__init__()
        self.config = config
        n_map = config["n_map"]
        norm = "GN"
        ng = 1

        keys = ["ctr", "norm", "ctr2", "left", "right"]
        for i in range(config["num_scales"]):
            keys.append("pre" + str(i))
            keys.append("suc" + str(i))

        fuse = dict()
        for key in keys:
            fuse[key] = []

        for i in range(4):
            for key in fuse:
                if key in ["norm"]:
                    fuse[key].append(nn.GroupNorm(gcd(ng, n_map), n_map))
                elif key in ["ctr2"]:
                    fuse[key].append(Linear(n_map, n_map, norm=norm, ng=ng, act=False))
                else:
                    fuse[key].append(nn.Linear(n_map, n_map, bias=False))

        for key in fuse:
            fuse[key] = nn.ModuleList(fuse[key])
        self.fuse = nn.ModuleDict(fuse)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, feat: Tensor, graph: Dict) -> Tensor:
        """fuse map"""
        res = feat
        for i in range(len(self.fuse["ctr"])):
            temp = self.fuse["ctr"][i](feat)
            for key in self.fuse:
                if key.startswith("pre") or key.startswith("suc"):
                    k1 = key[:3]
                    k2 = int(key[3:])
                    temp.index_add_(
                        0,
                        graph[k1][k2]["u"],
                        self.fuse[key][i](feat[graph[k1][k2]["v"]]),
                    )

            if len(graph["left"]["u"] > 0):
                temp.index_add_(
                    0,
                    graph["left"]["u"],
                    self.fuse["left"][i](feat[graph["left"]["v"]]),
                )
            if len(graph["right"]["u"] > 0):
                temp.index_add_(
                    0,
                    graph["right"]["u"],
                    self.fuse["right"][i](feat[graph["right"]["v"]]),
                )

            feat = self.fuse["norm"][i](temp)
            feat = self.relu(feat)

            feat = self.fuse["ctr2"][i](feat)
            feat += res
            feat = self.relu(feat)
            res = feat
        return feat


class M2A(nn.Module):
    """
    The lane to actor block fuses updated
        map information from lane nodes to actor nodes
    """
    def __init__(self, config):
        super(M2A, self).__init__()
        self.config = config
        norm = "GN"
        ng = 1

        n_actor = config["n_actor"]
        n_map = config["n_map"]

        att = []
        for i in range(2):
            att.append(Att(n_actor, n_map))
        self.att = nn.ModuleList(att)

    def forward(self, actors: Tensor, actor_idcs: List[Tensor], actor_ctrs: List[Tensor], nodes: Tensor, node_idcs: List[Tensor], node_ctrs: List[Tensor]) -> Tensor:
        for i in range(len(self.att)):
            actors = self.att[i](
                actors,
                actor_idcs,
                actor_ctrs,
                nodes,
                node_idcs,
                node_ctrs,
                self.config["map2actor_dist"],
            )
        return actors


class A2A(nn.Module):
    """
    The actor to actor block performs interactions among actors.
    """
    def __init__(self, config):
        super(A2A, self).__init__()
        self.config = config
        norm = "GN"
        ng = 1

        n_actor = config["n_actor"]
        n_map = config["n_map"]

        att = []
        for i in range(2):
            att.append(Att(n_actor, n_actor))
        self.att = nn.ModuleList(att)

    def forward(self, actors: Tensor, actor_idcs: List[Tensor], actor_ctrs: List[Tensor]) -> Tensor:
        for i in range(len(self.att)):
            actors = self.att[i](
                actors,
                actor_idcs,
                actor_ctrs,
                actors,
                actor_idcs,
                actor_ctrs,
                self.config["actor2actor_dist"],
            )
        return actors


class EncodeDist(nn.Module):
    def __init__(self, n, linear=True):
        super(EncodeDist, self).__init__()
        norm = "GN"
        ng = 1

        block = [nn.Linear(2, n), nn.ReLU(inplace=True)]

        if linear:
            block.append(nn.Linear(n, n))

        self.block = nn.Sequential(*block)

    def forward(self, dist):
        x, y = dist[:, :1], dist[:, 1:]
        dist = torch.cat(
            (
                torch.sign(x) * torch.log(torch.abs(x) + 1.0),
                torch.sign(y) * torch.log(torch.abs(y) + 1.0),
            ),
            1,
        )

        dist = self.block(dist)
        return dist


class PredNet(nn.Module):
    """
    Final motion forecasting with Linear Residual block
    """
    def __init__(self, config):
        super(PredNet, self).__init__()
        self.config = config
        norm = "GN"
        ng = 1

        n_actor = config["n_actor"]
        f_d = config["f_d"]

        pred = []
        for i in range(config["num_mods"]):
            pred.append(
                nn.Sequential(
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    nn.Linear(n_actor, 2 * config["num_preds"]),
                )
            )
        self.pred = nn.ModuleList(pred)
        
        un = []
        for i in range(1):
            un.append(
                nn.Sequential(
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    nn.Linear(n_actor, f_d * config["num_preds"]),
                )
            )
        self.un = nn.ModuleList(un)
        zg = []
        for i in range(1):
            zg.append(
                nn.Sequential(
                    LinearRes(n_actor, n_actor, norm=norm, ng=ng),
                    nn.Linear(n_actor, config["num_preds"]),
                )
            )
        self.zg = nn.ModuleList(zg)

        self.att_dest = AttDest(n_actor)
        self.att_dest2 = AttDest2(n_actor)

    def forward(self, actors: Tensor, actor_idcs: List[Tensor], actor_ctrs: List[Tensor]) -> Dict[str, List[Tensor]]:

        preds = []
        for i in range(len(self.pred)):
            preds.append(self.pred[i](actors))
        reg_t = torch.cat([x.unsqueeze(1) for x in preds], 1)
        reg = reg_t.view(reg_t.size(0), reg_t.size(1), -1, 2)

        for i in range(len(actor_idcs)):
            idcs = actor_idcs[i]
            ctrs = actor_ctrs[i].view(-1, 1, 1, 2)
            reg[idcs] = reg[idcs] + ctrs

        dest_ctrs = reg[:, :, -1].detach()
        
        uns, zs = [], []
        for i in range(len(self.pred)):
            dest_ctrs_i = dest_ctrs[:,i,:]
            featsunz = self.att_dest(actors, torch.cat(actor_ctrs, 0), dest_ctrs_i)
            ztt = torch.exp(self.zg[0](featsunz))
            zs.append(ztt)
            zd = ztt.detach()
            featsunza = self.att_dest2(zd, actors)
            uns.append(self.un[0](featsunza))
        uncertaintyt = torch.cat([x.unsqueeze(1) for x in uns], 1)
        zt = torch.cat([x.unsqueeze(1) for x in zs], 1)

        
        uncertainty = uncertaintyt.reshape(uncertaintyt.size(0), uncertaintyt.size(1), -1, config["f_d"])
        uncertainty = torch.exp(-torch.abs(uncertainty)) * 0.3
        
        z = zt.reshape(uncertaintyt.size(0), uncertaintyt.size(1), -1)
        
        out = dict()
        out["zs"], out["reg"] = [], []
        out["uncertainty"] = []
        for i in range(len(actor_idcs)):
            idcs = actor_idcs[i]
            temp_un = uncertainty[idcs]
            tz = z[idcs]
            tempr = reg[idcs]
            cls_i = tz[0,:,-1]
            cls_i = cls_i.unsqueeze(0).repeat(tempr.size(0),1)
            cls_i, sort_idcs = cls_i.sort(1, descending=False)
            row_idcs = torch.arange(len(sort_idcs)).long().to(sort_idcs.device)
            row_idcs = row_idcs.view(-1, 1).repeat(1, sort_idcs.size(1)).view(-1)
            sort_idcs = sort_idcs.view(-1)
            tempr = tempr[row_idcs, sort_idcs].view(cls_i.size(0), cls_i.size(1), -1, 2)
            tz = tz[row_idcs, sort_idcs].view(cls_i.size(0), cls_i.size(1), -1, 1)
            temp_un = temp_un[row_idcs, sort_idcs].view(cls_i.size(0), cls_i.size(1), -1, config["f_d"])
            out["uncertainty"].append(temp_un)
            out["reg"].append(tempr)
            out["zs"].append(tz)
        return out


class Att(nn.Module):
    """
    Attention block to pass context nodes information to target nodes
    This is used in Actor2Map, Actor2Actor, Map2Actor and Map2Map
    """
    def __init__(self, n_agt: int, n_ctx: int) -> None:
        super(Att, self).__init__()
        norm = "GN"
        ng = 1

        self.dist = nn.Sequential(
            nn.Linear(2, n_ctx),
            nn.ReLU(inplace=True),
            Linear(n_ctx, n_ctx, norm=norm, ng=ng),
        )

        self.query = Linear(n_agt, n_ctx, norm=norm, ng=ng)

        self.ctx = nn.Sequential(
            Linear(3 * n_ctx, n_agt, norm=norm, ng=ng),
            nn.Linear(n_agt, n_agt, bias=False),
        )

        self.agt = nn.Linear(n_agt, n_agt, bias=False)
        self.norm = nn.GroupNorm(gcd(ng, n_agt), n_agt)
        self.linear = Linear(n_agt, n_agt, norm=norm, ng=ng, act=False)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, agts: Tensor, agt_idcs: List[Tensor], agt_ctrs: List[Tensor], ctx: Tensor, ctx_idcs: List[Tensor], ctx_ctrs: List[Tensor], dist_th: float) -> Tensor:
        res = agts
        if len(ctx) == 0:
            agts = self.agt(agts)
            agts = self.relu(agts)
            agts = self.linear(agts)
            agts += res
            agts = self.relu(agts)
            return agts

        batch_size = len(agt_idcs)
        hi, wi = [], []
        hi_count, wi_count = 0, 0
        for i in range(batch_size):
            dist = agt_ctrs[i].view(-1, 1, 2) - ctx_ctrs[i].view(1, -1, 2)
            dist = torch.sqrt((dist ** 2).sum(2))
            mask = dist <= dist_th

            idcs = torch.nonzero(mask, as_tuple=False)
            if len(idcs) == 0:
                continue

            hi.append(idcs[:, 0] + hi_count)
            wi.append(idcs[:, 1] + wi_count)
            hi_count += len(agt_idcs[i])
            wi_count += len(ctx_idcs[i])
        hi = torch.cat(hi, 0)
        wi = torch.cat(wi, 0)

        agt_ctrs = torch.cat(agt_ctrs, 0)
        ctx_ctrs = torch.cat(ctx_ctrs, 0)
        dist = agt_ctrs[hi] - ctx_ctrs[wi]
        dist = self.dist(dist)

        query = self.query(agts[hi])

        ctx = ctx[wi]
        ctx = torch.cat((dist, query, ctx), 1)
        ctx = self.ctx(ctx)

        agts = self.agt(agts)
        agts.index_add_(0, hi, ctx)
        agts = self.norm(agts)
        agts = self.relu(agts)

        agts = self.linear(agts)
        agts += res
        agts = self.relu(agts)
        return agts


class AttDest(nn.Module):
    def __init__(self, n_agt: int):
        super(AttDest, self).__init__()
        norm = "GN"
        ng = 1

        self.dist = nn.Sequential(
            nn.Linear(2, n_agt),
            nn.ReLU(inplace=True),
            Linear(n_agt, n_agt, norm=norm, ng=ng),
        )

        self.agt = Linear(2 * n_agt, n_agt, norm=norm, ng=ng)

    def forward(self, agts: Tensor, agt_ctrs: Tensor, dest_ctrs: Tensor) -> Tensor:

        dist = (agt_ctrs - dest_ctrs)
        dist = self.dist(dist)

        agts = torch.cat((dist, agts), 1)
        agts = self.agt(agts)
        return agts

class AttDest2(nn.Module):
    def __init__(self, n_agt: int):
        super(AttDest2, self).__init__()
        norm = "GN"
        ng = 1
        self.en1 = nn.Sequential(
            nn.Linear(config["num_preds"], n_agt),
            nn.ReLU(inplace=True),
            Linear(n_agt, n_agt, norm=norm, ng=ng),
        )
        self.agt = Linear(2 * n_agt, n_agt, norm=norm, ng=ng)

    def forward(self, zs: Tensor, actorf: Tensor) -> Tensor:
        fzs = self.en1(zs)

        agts = torch.cat((fzs, actorf), 1)
        agts = self.agt(agts)
        return agts


class PredLoss(nn.Module):
    def __init__(self, config, co_un_l1, co_fun_l1):
        super(PredLoss, self).__init__()
        self.config = config
        self.reg_loss = lambda a,b,c,d,e,f:co_un_l1(a,b,c,d,e,f)
        self.freg_loss = lambda a,b,c,d,e,f:co_fun_l1(a,b,c,d,e,f)

    def forward(self, out: Dict[str, List[Tensor]], gt_preds: List[Tensor], has_preds: List[Tensor]) -> Dict[str, Union[Tensor, int]]:
        reg = out["reg"]
        uncertainty = out["uncertainty"]
        zs =  out["zs"]
        loss_out = dict()
        loss_out["cls_loss"] = 0.0
        loss_out["num_cls"] = 0.0
        loss_out["reg_loss"] = 0.0
        loss_out["num_reg"] = 0.0
        
        for i in range(len(reg)):
            reg_i = reg[i]
            zs_i = zs[i]
            uncertainty_i = uncertainty[i]
            gt_preds_i = gt_preds[i]
            has_preds_i = has_preds[i]
            
            num_mods, num_preds = self.config["num_mods"], self.config["num_preds"]

            last = has_preds_i.float() + 0.1 * torch.arange(num_preds).float().to(
                has_preds_i.device
             ) / float(num_preds)
            max_last, last_idcs = last.max(1)
            mask = max_last > 1.0
            reg_i = reg_i[mask]
            zs_i = zs_i[mask]
            uncertainty_i = uncertainty_i[mask]
            gt_preds_i = gt_preds_i[mask]
            has_preds_i = has_preds_i[mask]
            last_idcs = last_idcs[mask]
            row_idcs = torch.arange(len(last_idcs)).long().to(last_idcs.device)
            distf = []
            for j in range(num_mods):
                distf.append(
                    torch.sqrt(
                        (
                            (reg_i[row_idcs, j, last_idcs] - gt_preds_i[row_idcs, last_idcs])
                            ** 2
                        ).sum(1)
                    )
                )
            distf = torch.cat([x.unsqueeze(1) for x in distf], 1)
            min_dist, min_idcs = distf.min(1)
            row_idcs = torch.arange(len(min_idcs)).long().to(min_idcs.device)
            
            has_preds_i = has_preds_i.unsqueeze(-1)
            has_preds_i = has_preds_i * 1
            has_preds_i = has_preds_i.unsqueeze(1).repeat(1,6,1,1)
            gt_preds_i = gt_preds_i.unsqueeze(1).repeat(1,6,1,1)
            dist_i = gt_preds_i - reg_i
            dist_i = dist_i * has_preds_i
            dist_i = torch.abs(dist_i)
            zs_i = zs_i * has_preds_i
            coef = self.config["cls_coef"]
            loss_out["cls_loss"] += coef * 0.0
            
            rcoef = self.config["reg_coef"]

            has_preds_i = has_preds_i[row_idcs, min_idcs]
            uncertainty_i = uncertainty_i[row_idcs, min_idcs]
            zs_i = zs_i[row_idcs, min_idcs]
            dist = dist_i[row_idcs, min_idcs]
            uncertainty_i = uncertainty_i * has_preds_i
            num_a = has_preds_i.sum(0).squeeze(-1)
            dist = dist.sum(-1)
            zs_i = zs_i.sum(0).squeeze(-1)
            zs_i = zs_i / num_a
            dist = dist.unsqueeze(-1)
            dist_l = dist.permute(1,2,0)
            dist_r = dist_l.permute(0,2,1)
            l_un_u = uncertainty_i.permute(1,0,2)
            r_un_u = l_un_u.permute(0,2,1)
            unu = l_un_u  @ r_un_u
            unu = unu / config["f_d"]
            iep = 0.05 * torch.eye(unu.size(-1))
            iep = iep.unsqueeze(0)
            iep = iep.repeat(unu.size(0),1,1)
            iep = iep.cuda().detach()
            unu = unu + iep
            f_weight = self.config["f_weight"]
            loss_out["reg_loss"] += rcoef * self.reg_loss(
                dist_l, dist_r, unu, uncertainty_i, zs_i, num_a
            )
            loss_out["reg_loss"] += f_weight * rcoef * self.freg_loss(
                dist_l[-1], dist_r[-1], unu[-1], uncertainty_i[:,-1,:], zs_i[-1], num_a[-1]
            )
            loss_out["num_reg"] += has_preds_i.sum().item()
            loss_out["num_cls"] += has_preds_i.sum().item()
        return loss_out


class Loss(nn.Module):
    def __init__(self, config, co_un_l1, co_fun_l1):
        super(Loss, self).__init__()
        self.config = config
        self.pred_loss = PredLoss(config, co_un_l1, co_fun_l1)

    def forward(self, out: Dict, data: Dict) -> Dict:
        loss_out = self.pred_loss(out, gpu(data["gt_preds"]), gpu(data["has_preds"]))
        loss_out["loss"] = loss_out["cls_loss"] / (
            loss_out["num_cls"] + 1e-10
        ) + loss_out["reg_loss"] / (loss_out["num_reg"] + 1e-10)
        return loss_out


class PostProcess(nn.Module):
    def __init__(self, config):
        super(PostProcess, self).__init__()
        self.config = config

    def forward(self, out,data):
        post_out = dict()
        post_out["preds"] = [x[0:1].detach().cpu().numpy() for x in out["reg"]]
        post_out["gt_preds"] = [x[0:1].numpy() for x in data["gt_preds"]]
        post_out["has_preds"] = [x[0:1].numpy() for x in data["has_preds"]]
        return post_out

    def append(self, metrics: Dict, loss_out: Dict, post_out: Optional[Dict[str, List[ndarray]]]=None) -> Dict:
        if len(metrics.keys()) == 0:
            for key in loss_out:
                if key != "loss":
                    metrics[key] = 0.0

            for key in post_out:
                metrics[key] = []

        for key in loss_out:
            if key == "loss":
                continue
            if isinstance(loss_out[key], torch.Tensor):
                metrics[key] += loss_out[key].item()
            else:
                metrics[key] += loss_out[key]

        for key in post_out:
            metrics[key] += post_out[key]
        return metrics

    def display(self, metrics, dt, epoch, lr=None):
        """Every display-iters print training/val information"""
        if lr is not None:
            print("Epoch %3.3f, lr %.5f, time %3.2f" % (epoch, lr, dt))
        else:
            print(
                "************************* Validation, time %3.2f *************************"
                % dt
            )

        cls = metrics["cls_loss"] / (metrics["num_cls"] + 1e-10)
        reg = metrics["reg_loss"] / (metrics["num_reg"] + 1e-10)
        loss = cls + reg

        preds = np.concatenate(metrics["preds"], 0)
        gt_preds = np.concatenate(metrics["gt_preds"], 0)
        has_preds = np.concatenate(metrics["has_preds"], 0)
        ade1, fde1, ade, fde, min_idcs = pred_metrics(preds, gt_preds, has_preds)

        print(
            "loss %2.4f %2.4f %2.4f, ade1 %2.4f, fde1 %2.4f, ade %2.4f, fde %2.4f"
            % (loss, cls, reg, ade1, fde1, ade, fde)
        )
        print()


def pred_metrics(preds, gt_preds, has_preds):
    assert has_preds.all()
    preds = np.asarray(preds, np.float32)
    gt_preds = np.asarray(gt_preds, np.float32)

    """batch_size x num_mods x num_preds"""
    err = np.sqrt(((preds - np.expand_dims(gt_preds, 1)) ** 2).sum(3))

    ade1 = err[:, 0].mean()
    fde1 = err[:, 0, -1].mean()

    min_idcs = err[:, :, -1].argmin(1)
    row_idcs = np.arange(len(min_idcs)).astype(np.int64)
    err = err[row_idcs, min_idcs]
    ade = err.mean()
    fde = err[:, -1].mean()
    return ade1, fde1, ade, fde, min_idcs


def get_model():
    net = Net(config)
    net = net.cuda()

    loss = Loss(config, co_un_l1, co_fun_l1).cuda()
    post_process = PostProcess(config).cuda()

    params = net.parameters()
    opt = Optimizer(params, config)


    return config, ArgoDataset, collate_fn, net, loss, post_process, opt

def co_un_l1(dl, dr, un, uncertainty, z, num_a):
    uncertainty_mask = (uncertainty == 0) * 1
    uncertainty = uncertainty + uncertainty_mask
    uncertainty = 2 * ((torch.log(uncertainty)).sum(-1)) / config["f_d"]
    uncertainty = uncertainty.sum(0)
    loss = dl @ un @ dr
    loss = loss.squeeze(-1).squeeze(-1)
    loss = (loss / z + num_a * torch.log(z) - uncertainty) / 2
    loss = torch.sum(loss)
    return loss

def co_fun_l1(dl, dr, un, uncertainty, z, num_a):
    uncertainty_mask = (uncertainty == 0) * 1
    uncertainty = uncertainty + uncertainty_mask
    uncertainty = 2 * ((torch.log(uncertainty)).sum(-1)) / config["f_d"]
    loss = dl @ un @ dr
    loss = loss.squeeze(-1).squeeze(-1)
    loss = (loss / z + num_a * torch.log(z) - uncertainty.sum(0)) / 2
    loss = torch.sum(loss)
    return loss
