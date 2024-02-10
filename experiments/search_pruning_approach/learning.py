from contextlib import nullcontext
from functools import cache
import time
import numpy as np
import numpy.typing as npt
from dataclasses import dataclass
import itertools
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from torch import nn, Tensor, tensor
import torch
from predicators.settings import CFG
import logging

from predicators.structs import NSRT, _GroundNSRT, State, Variable

import matplotlib.pyplot as plt
import matplotlib
# matplotlib.use("tkagg")

@dataclass(frozen=True)
class FeasibilityDatapoint:
    """Holds the sequence of states and skeleton that
    would have been generated by the backtracking search.
    Used to train the classifier.
    """
    states: Sequence[State]
    skeleton: Sequence[_GroundNSRT]

    def __post_init__(self) -> None:
        assert 0 <= len(self.states) - 1 <= len(self.skeleton)

class FeasibilityDataset(torch.utils.data.Dataset):
    """Takes the feasibility datapoints and outputs the encoder and decoder NSRTs,
    the state vectors associated with them and the label for whether it is a positive or a negative example.
    """
    def __init__(
        self,
        positive_examples: Sequence[FeasibilityDatapoint],
        negative_examples: Sequence[FeasibilityDatapoint]
    ):
        super().__init__()
        assert all(
            2 <= len(datapoint.states) <= len(datapoint.skeleton) # should be at least one encoder and decoder nsrt
            for datapoint in positive_examples + negative_examples
        )
        self._positive_examples = positive_examples
        self._negative_examples = negative_examples
        self._total_label_examples = max(len(positive_examples), len(negative_examples))

    def __len__(self) -> int:
        return self._total_label_examples * 2

    @cache
    def __getitem__(self, idx: int) -> Tuple[Tuple[List[NSRT], List[NSRT], List[npt.NDArray], List[npt.NDArray]], int]:
        if idx < self._total_label_examples:
            return self.transform_datapoint(self._positive_examples[idx % len(self._positive_examples)]), 1.0
        elif idx < self._total_label_examples * 2:
            return self.transform_datapoint(
                self._negative_examples[(idx - self._total_label_examples) % len(self._negative_examples)]
            ), 0.0
        else:
            raise IndexError()

    @classmethod
    def transform_datapoint(
        cls, datapoint: FeasibilityDatapoint
    ) -> Tuple[Tuple[List[NSRT], List[NSRT], List[npt.NDArray], List[npt.NDArray]]]:
        """Helper function that splits a feasibility datapoint into the encoder and decoder nsrts and state vectors
        """
        prefix_length = len(datapoint.states) - 1

        return [ground_nsrt.parent for ground_nsrt in datapoint.skeleton[:prefix_length]], \
            [ground_nsrt.parent for ground_nsrt in datapoint.skeleton[prefix_length:]], [
                state[ground_nsrt.objects]
                for state, ground_nsrt in zip(datapoint.states[1:], datapoint.skeleton)
            ], [
                datapoint.states[-1][ground_nsrt.objects]
                for ground_nsrt in datapoint.skeleton[prefix_length:]
            ]

class FeasibilityFeaturizer(nn.Module):
    """Featurizer that turns a state vector into a uniformly sized feature vector.
    """
    def __init__(
        self,
        state_vector_size: int,
        hidden_sizes: List[int],
        feature_size: int,
        device: Optional[str] = None
    ):
        """Creates a new featurizer

        Params:
            state_vector_size - size of the state vector for the corresponding NSRT
            hidden_sizes - the sizes of the hidden layers in the DNN
            feature_size - output feature vector size
            device (optional) - what device to place the module and generated vectors on
                (uses the globally default device if unspecified)
        """
        super().__init__()
        self._device = device
        self._range = None

        sizes = [state_vector_size] + hidden_sizes + [feature_size]
        self._layers = nn.ModuleList([
            nn.Linear(input_size, output_size, device=device)
            for input_size, output_size in zip(sizes, sizes[1:])
        ])

    def update_range(self, state: npt.NDArray) -> None:
        state = tensor(state, device=self._device)
        if self._range is None:
            self._range = (state, state)
        else:
            min_state, max_state = self._range
            self._range = (torch.minimum(min_state, state), torch.maximum(max_state, state))

    def forward(self, state: npt.NDArray) -> Tensor:
        """Runs the featurizer

        Params:
            state - numpy array of shape (batch_size, state_vector_size) of batched state vectors
        """
        state = tensor(state, device=self._device)

        if self._range is not None:
            min_state, max_state = self._range
            state -= min_state
            state /= torch.clamp(max_state - min_state, min=0.1)

        for layer in self._layers[:-1]:
            state = nn.functional.elu(layer(state))
        return self._layers[-1](state)

class PositionalEmbeddingLayer(nn.Module):
    """Adds a positional embedding and optionally a cls token to the feature vectors"""
    def __init__(
        self,
        feature_size: int,
        embedding_size: int,
        concat: bool,
        include_cls: Optional[str],
        horizon: int,
        device: Optional[str] = None
    ):
        """Creates a new positional embedding layer.

        Params:
            feature_size - size of the output feature vector
            embedding_size - size of the embedding vector (if it's concatenated)
            concat - whether the positional embedding should be concatenated with the input vector
                or added to it
            include_cls - whether and how the embedding vector should be included
                before all the feature vectors. Possible values are: None (no cls), 'learned'
                (as a learnable parameter), 'marked' (each feature vector has an additional 0 added to it
                and the cls token has a 1 in that space)

        """
        super().__init__()
        assert include_cls in ['learned', 'marked', None]
        self._device = device
        self._concat = concat
        self._horizon = horizon

        if concat:
            self._input_size = feature_size - embedding_size - (include_cls == 'marked')
            self._embedding_size = embedding_size
        else:
            self._input_size = feature_size - (include_cls == 'marked')
            self._embedding_size = self._input_size

        if include_cls == 'learned':
            self._cls = nn.Parameter(torch.randn((1, feature_size), device=device))
            self._cls_marked = False
        elif include_cls == 'marked':
            self._cls = torch.concat([torch.zeros((1, feature_size - 1), device=device), torch.ones((1, 1), device=device)], dim=1)
            self._cls_marked = True
        else:
            self._cls = None
            self._cls_marked = False

    @property
    def input_size(self) -> int:
        return self._input_size

    def forward(self, tokens: Tensor, pos_offset: Optional[Tensor] = None) -> Tensor:
        """Runs the positional embeddings.

        Params:
            tokens - tensor of shape (batch_size, max_sequence_length, vector_input_size) of outputs from featurizers
            pos_offset - tensor of shape (batch_size,) of positions offsets per batch
        where `vector_input_size` = `feature_size` [- `embedding_size` if `concat`] [- 1 if `include_cls` is `marked`]
        """

        batch_size, max_len, input_feature_size = tokens.shape
        assert input_feature_size == self._input_size

        # Calculating per-token positions and in-token indices
        indices = torch.arange(self._embedding_size, device=self._device).unsqueeze(0)
        positions = torch.arange(max_len, device=self._device).unsqueeze(0).unsqueeze(-1).expand(batch_size, -1, -1)
        if pos_offset is not None:
            positions = positions + pos_offset.unsqueeze(-1).unsqueeze(-1)

        # Calculating the embeddings
        freq = 1 / self._horizon ** ((indices - (indices % 2)) / self._embedding_size)
        embeddings = torch.sin(positions.float() @ freq + torch.pi / 2 * (indices % 2))

        # Concateanting/adding embeddings
        if self._concat:
            embedded_tokens = torch.cat([tokens, embeddings], dim=-1)
        else:
            embedded_tokens = tokens + embeddings

        # Adding the cls token
        if self._cls is None:
            return embedded_tokens
        elif self._cls_marked:
            marked_tokens = torch.cat([embedded_tokens, torch.zeros((batch_size, max_len, 1), device=self._device)], dim=2)
            return torch.cat([self._cls.unsqueeze(0).expand(batch_size, -1, -1), marked_tokens], dim=1)
        else:
            return torch.cat([self._cls.unsqueeze(0).expand(batch_size, -1, -1), embedded_tokens], dim=1)

    def recalculate_mask(self, mask: Tensor) -> Tensor:
        """Recalculates the mask to include the cls token

        Params:
            mask - boolean tensor of shape (batch_size, max_sequence_length)
        """
        if self._cls is None:
            return mask
        return torch.cat([torch.full((mask.shape[0], 1), False, device=self._device), mask], dim=1)

FeasibilityClassifier = Callable[[Sequence[State], Sequence[_GroundNSRT]], Tuple[bool, float]]

def sigmoid_focal_loss(
    inputs: Tensor,
    targets: Tensor,
    alpha: float = 0.25,
    gamma_pos: float = 3,
    gamma_neg: float = 2,
    reduction: str = "none",
) -> Tensor:
    """Adapted from torchvision.ops.focal_loss"""
    ce_loss = nn.functional.binary_cross_entropy(inputs, targets, reduction="none")

    loss = torch.zeros_like(ce_loss)
    is_positive = torch.abs(targets - 1.0) < 0.0001
    is_negative = torch.logical_not(is_positive)
    loss[is_positive] = ce_loss[is_positive] * ((1 - inputs[is_positive]) ** gamma_pos)
    loss[is_negative] = ce_loss[is_negative] * (inputs[is_negative] ** gamma_neg)

    if alpha >= 0:
        alpha_t = alpha * targets + (1 - alpha) * (1 - targets)
        loss = alpha_t * loss

    assert reduction in {"none", "mean", "sum"}
    if reduction == "mean":
        loss = loss.mean()
    elif reduction == "sum":
        loss = loss.sum()
    return loss

class NeuralFeasibilityClassifier(nn.Module):
    # +--------+     +--------+  +----------+     +--------+
    # |   ENC  |     |   ENC  |  |   DEC    |     |   DEC  |
    # \ FEAT 1 / ... \ FEAT N /  \ FEAT N+1 / ... \ FEAT M /
    #  \______/       \______/    \________/       \______/
    #     ||             ||           ||              ||
    #     \/             \/           \/              \/
    # +-----------------------+  +-------------------------+
    # |        ENCODER        |->|         DECODER         |
    # +-----------------------+  +-------------------------+
    #                               ||   ||   ||   ||   ||
    #                               \/   \/   \/   \/   \/
    #                            +-------------------------+
    #                             \     MEAN POLLING      /
    #                              +---------------------+
    #                                       |  |
    #                                       \__/
    #                              +---------------------+
    #                               \    CLASSIFIER     /
    #                                +-----------------+
    def __init__(
        self, # TODO: make sure we use a deterministic seed
        seed: int,
        featurizer_hidden_sizes: List[int],
        classifier_feature_size: int,
        positional_embedding_size: int,
        positional_embedding_concat: bool,
        transformer_num_heads: int,
        transformer_encoder_num_layers: int,
        transformer_decoder_num_layers: int,
        transformer_ffn_hidden_size: int,
        max_train_iters: int,
        general_lr: float,
        transformer_lr: float,
        max_inference_suffix: int,
        cls_style: str,
        embedding_horizon: int,
        batch_size: int,
        threshold_recalibration_percentile: float,
        test_split: float = 0.125,
        dropout: int = 0.25,
        num_threads: int = 8,
        classification_threshold: float = 0.5,
        device: Optional[str] = 'cuda',
        check_nans: bool = False,
    ):
        torch.manual_seed(seed)
        torch.set_num_threads(num_threads)
        super().__init__()
        self._device = device

        assert cls_style in {'learned', 'marked', 'mean'}
        self._cls_style = cls_style

        self._max_inference_suffix = max_inference_suffix
        self._thresh = classification_threshold

        self._num_iters = max_train_iters
        self._batch_size = batch_size
        self._test_split = test_split
        self._general_lr = general_lr
        self._transformer_lr = transformer_lr

        self._encoder_featurizers: Dict[NSRT, FeasibilityFeaturizer] = {} # Initialized with self._init_featurizer
        self._decoder_featurizers: Dict[NSRT, FeasibilityFeaturizer] = {} # Initialized with self._init_featurizer
        self._featurizer_hidden_sizes = featurizer_hidden_sizes
        self._featurizer_count: int = 0 # For naming the module when adding it in self._init_featurizer

        self._encoder_positional_encoding = PositionalEmbeddingLayer(
            feature_size = classifier_feature_size,
            embedding_size = positional_embedding_size,
            concat = positional_embedding_concat,
            include_cls = None,
            horizon = embedding_horizon,
            device = device,

        )
        self._decoder_positional_encoding = PositionalEmbeddingLayer(
            feature_size = classifier_feature_size,
            embedding_size = positional_embedding_size,
            concat = positional_embedding_concat,
            include_cls = {
                'mean': None, 'learned': 'learned', 'marked': 'marked'
            }[cls_style],
            horizon = embedding_horizon,
            device = device,
        )
        self._transformer = nn.Transformer(
            d_model = classifier_feature_size,
            nhead = transformer_num_heads,
            num_encoder_layers = transformer_encoder_num_layers,
            num_decoder_layers = transformer_decoder_num_layers,
            dim_feedforward = transformer_ffn_hidden_size,
            dropout = dropout,
            batch_first = True,
            device = device,
        )
        self._classifier_head = nn.Sequential(
            nn.Linear(classifier_feature_size, 1, device=device),
            nn.Sigmoid(),
        )

        self._optimizer: Optional[torch.optim.Optimizer] = None
        self._check_nans = check_nans
        self._threshold_recalibration_frac = threshold_recalibration_percentile
        self._unsure_confidence = 1.0

    def classify(self, states: Sequence[State], skeleton: Sequence[_GroundNSRT]) -> Tuple[bool, float]:
        """Classifies a single datapoint
        """
        if len(states) == len(skeleton) + 1: # Make sure there is at least one decoder nsrt
            return True, 1.0
        if len(skeleton) - self._max_inference_suffix >= len(states): # Make sure we don't have too big of a horizon to predict
            return True, self._unsure_confidence

        encoder_nsrts, decoder_nsrts, encoder_states, decoder_states = \
            FeasibilityDataset.transform_datapoint(FeasibilityDatapoint(states, skeleton))
        self._init_featurizers_datapoint(encoder_nsrts, decoder_nsrts, encoder_states, decoder_states)

        self.eval()
        confidence = self([encoder_nsrts], [decoder_nsrts], [encoder_states], [decoder_states]).cpu()
        print(f"Confidence {float(confidence)}")
        return confidence >= self._thresh, confidence

    def fit(
        self,
        positive_examples: Sequence[FeasibilityDatapoint],
        negative_examples: Sequence[FeasibilityDatapoint]
    ) -> None:
        self._create_optimizer()
        if not positive_examples and not negative_examples:
            return

        logging.info(f"Training Feasibility Classifier from {len(positive_examples)} "
                     f"positive and {len(negative_examples)} negative datapoints...")

        # Creating datasets
        logging.info(f"Creating train and test datasets (test split {int(self._test_split*100)}%)")

        positive_examples, negative_examples = positive_examples.copy(), negative_examples.copy()
        np.random.shuffle(positive_examples)
        np.random.shuffle(negative_examples)

        positive_train_size = len(positive_examples) - int(len(positive_examples) * self._test_split)
        negative_train_size = len(negative_examples) - int(len(negative_examples) * self._test_split)
        train_dataset = FeasibilityDataset(positive_examples[:positive_train_size], negative_examples[:negative_train_size])
        test_dataset = FeasibilityDataset(positive_examples[positive_train_size:], negative_examples[negative_train_size:])


        # Initializing per-nsrt featurizers if not initialized already
        logging.info("Initializing state featurizers")
        self._init_featurizers_dataset(train_dataset)
        self._init_featurizers_dataset(test_dataset)

        # Setting up dataloaders
        train_dataloader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=self._batch_size,
            shuffle=True,
            collate_fn=self._collate_batch
        )
        test_dataloader = torch.utils.data.DataLoader(
            test_dataset,
            batch_size=self._batch_size,
            collate_fn=self._collate_batch
        )

        # Creating loss functions
        train_loss_fn = lambda inputs, targets: sigmoid_focal_loss(inputs, targets, reduction="mean")
        test_loss_fn = nn.BCELoss()

        # Training loop
        logging.info("Running training")
        with (torch.autograd.detect_anomaly(True) if self._check_nans else nullcontext()):
            for itr, (x_train_batch, y_train_batch) in zip(range(self._num_iters), itertools.cycle(train_dataloader)):
                self.train()
                self._optimizer.zero_grad()
                train_loss = train_loss_fn(self(*x_train_batch), y_train_batch)
                train_loss.backward()
                self._optimizer.step()
                if itr % 100 == 0:
                    # Evaluating on a test dataset
                    self.eval()
                    y_pred_batches, y_true_batches = zip(*(
                        (self(*x_test_batch), y_test_batch)
                        for x_test_batch, y_test_batch in test_dataloader
                    ))
                    y_pred, y_true = torch.concatenate(y_pred_batches), torch.concatenate(y_true_batches)

                    # Calculating the loss and accuracy
                    test_loss = float(test_loss_fn(y_pred, y_true).cpu().detach())
                    matches = torch.logical_or(
                        torch.logical_and(torch.abs(y_true - 0.0) < 0.0001, y_pred <= self._thresh),
                        torch.logical_and(torch.abs(y_true - 1.0) < 0.0001, y_pred >= self._thresh)
                    ).cpu().detach().numpy()

                    # Calculating additional metrics
                    num_false_positives = torch.logical_and(torch.abs(y_true - 0.0) < 0.0001, y_pred >= self._thresh).cpu().detach().numpy().sum()
                    num_false_negatives = torch.logical_and(torch.abs(y_true - 1.0) < 0.0001, y_pred <= self._thresh).cpu().detach().numpy().sum()

                    false_positive_confidence = float(torch.kthvalue(torch.cat([y_pred[
                        torch.logical_and(torch.abs(y_true - 0.0) < 0.0001, y_pred >= self._thresh)
                    ].flatten(), tensor([0], device=self._device)]).cpu(), int(
                        num_false_positives * self._threshold_recalibration_frac
                    ) + 1).values)

                    acceptance_rate = (
                        torch.logical_and(torch.abs(y_true - 1.0) < 0.0001, y_pred >= false_positive_confidence).cpu().detach().numpy().sum() /
                        (torch.abs(y_true - 1.0) < 0.0001).cpu().detach().numpy().sum()
                    )

                    logging.info(f"Loss: {test_loss}, Acc: {matches.mean():.1%}, "
                                f"%False+: {num_false_positives/len(y_true):.1%}, %False-: {num_false_negatives/len(y_true):.1%}, "
                                f"{self._threshold_recalibration_frac:.0%} False+ Thresh: {false_positive_confidence:.4}, Acceptance rate: {acceptance_rate:.1%}, "
                                f"Training Iter {itr}/{self._num_iters}")

                    if matches.mean() >= 0.99:
                        break

        if CFG.feasibility_loss_output_file:
            test_loss = np.concatenate([
                test_loss_fn(self(*x_test_batch), y_test_batch).detach().numpy()
                for x_test_batch, y_test_batch in test_dataloader
            ]).mean()
            print(test_loss, file=open(CFG.feasibility_loss_output_file, "w"))
            raise RuntimeError()

        # Threshold recalibration
        y_pred_batches, y_true_batches = zip(*(
            (self(*x_test_batch), y_test_batch)
            for x_test_batch, y_test_batch in test_dataloader
        ))
        y_pred, y_true = torch.concatenate(y_pred_batches), torch.concatenate(y_true_batches)

        num_false_positives = torch.logical_and(torch.abs(y_true - 0.0) < 0.0001, y_pred >= self._thresh).cpu().detach().numpy().sum()

        false_positive_confidence = float(torch.kthvalue(torch.cat([y_pred[
            torch.logical_and(torch.abs(y_true - 0.0) < 0.0001, y_pred >= self._thresh)
        ].flatten(), tensor([0], device=self._device)]).cpu(), int(
            num_false_positives * self._threshold_recalibration_frac
        ) + 1).values)

        self._unsure_confidence = max(self._thresh, false_positive_confidence)
        logging.info(f"Updated the threshold of the classifer to {self._thresh}")

    def _collate_batch(
        self, batch: Sequence[Tuple[Tuple[Sequence[NSRT], Sequence[NSRT], Sequence[npt.NDArray], Sequence[npt.NDArray]], int]]
    ) -> Tuple[Tuple[Sequence[Sequence[NSRT]], Sequence[Sequence[NSRT]], Sequence[Sequence[npt.NDArray]], Sequence[Sequence[npt.NDArray]]], Tensor]:
        """ Convert a batch of datapoints to batched datapoints
        """
        return (
            [dp[0][0] for dp in batch], [dp[0][1] for dp in batch],
            [dp[0][2] for dp in batch], [dp[0][3] for dp in batch]
        ), tensor([dp[1] for dp in batch], device=self._device)

    def _create_optimizer(self):
        if self._optimizer is None:
            self._optimizer = torch.optim.Adam([
                {'params':
                    list(self._encoder_positional_encoding.parameters()) +
                    list(self._decoder_positional_encoding.parameters())
                , 'lr': self._general_lr},
                {'params': self._classifier_head.parameters(), 'lr': self._general_lr},
                {'params': self._transformer.parameters(), 'lr': self._transformer_lr},
            ])

    def forward(
        self,
        encoder_nsrts_batch: Sequence[Sequence[NSRT]],
        decoder_nsrts_batch: Sequence[Sequence[NSRT]],
        encoder_states_batch: Sequence[Sequence[npt.NDArray]],
        decoder_states_batch: Sequence[Sequence[npt.NDArray]],
    ) -> float:
        """Runs the core of the classifier with given encoder and decoder nsrts and state vectors.
        """
        assert len(encoder_nsrts_batch) == len(decoder_nsrts_batch) and\
            len(decoder_nsrts_batch) == len(encoder_states_batch) and\
            len(encoder_states_batch) == len(decoder_states_batch)
        assert all(
            len(encoder_states) == len(encoder_nsrts)
            for encoder_states, encoder_nsrts in zip(encoder_states_batch, encoder_nsrts_batch)
        )
        max_skeleton_length = max(
            len(encoder_nsrts) + len(decoder_nsrts)
            for encoder_nsrts, decoder_nsrts
            in zip(encoder_nsrts_batch, decoder_nsrts_batch)
        )

        # Calculating feature vectors
        encoder_tokens, encoder_mask = self._run_featurizers(
            self._encoder_featurizers, self._encoder_positional_encoding.input_size, encoder_states_batch, encoder_nsrts_batch
        )
        decoder_tokens, decoder_mask = self._run_featurizers(
            self._decoder_featurizers, self._decoder_positional_encoding.input_size, decoder_states_batch, decoder_nsrts_batch
        )
        encoder_sequence_lengths = torch.logical_not(encoder_mask).sum(dim=1)
        decoder_sequence_lenghts = torch.logical_not(decoder_mask).sum(dim=1)

        # Calculating offsets for encoding
        decoder_offsets = encoder_sequence_lengths
        random_offsets = torch.zeros((decoder_offsets.shape[0],), device=self._device)
        if self.training:
            random_offsets = (
                torch.rand((decoder_offsets.shape[0],), device=self._device) *
                (self._decoder_positional_encoding._horizon - encoder_sequence_lengths - decoder_sequence_lenghts)
            ).long()

        # Adding positional encoding and cls token
        encoder_positional_tokens = self._encoder_positional_encoding(encoder_tokens, random_offsets)
        decoder_positional_tokens = self._decoder_positional_encoding(decoder_tokens, decoder_offsets + random_offsets)

        encoder_positional_mask = self._encoder_positional_encoding.recalculate_mask(encoder_mask)
        decoder_positional_mask = self._decoder_positional_encoding.recalculate_mask(decoder_mask)

        # Running the core transformer
        transformer_outputs = self._transformer(
            src=encoder_positional_tokens,
            tgt=decoder_positional_tokens,
            src_key_padding_mask=encoder_positional_mask,
            tgt_key_padding_mask=decoder_positional_mask,
            src_is_causal=False,
            tgt_is_causal=False,
        )

        # Preparing for the classifier head
        if self._cls_style == 'mean':
            transformer_outputs[decoder_mask.unsqueeze(-1).expand(-1, -1, transformer_outputs.shape[2])] = 0
            classifier_tokens = transformer_outputs.sum(dim=1) / decoder_sequence_lenghts.unsqueeze(-1)
        else:
            classifier_tokens = transformer_outputs[:, 0]

        # Running the classifier head
        output = self._classifier_head(classifier_tokens).flatten()

        return output

    def _run_featurizers(
        self,
        featurizers: Dict[NSRT, nn.Module],
        output_size: int,
        states_batch: Iterable[Sequence[npt.NDArray]],
        nsrts_batch: Iterable[Iterable[NSRT]],
    ) -> Tuple[Tensor, Tensor]:
        """ Runs state featurizers that are executed before passing the states into the main transformer

        Outputs the transformer inputs and the padding mask for them
        """
        batch_size = len(states_batch)
        max_len = max(len(states) for states in states_batch)

        # Preparing to batch the execution of featurizers
        tokens = torch.zeros((batch_size, max_len, output_size), device=self._device)
        mask = torch.full((batch_size, max_len), True, device='cpu') # Not on device for fast assignment

        # Grouping data for batched execution of featurizers
        grouped_data: Dict[NSRT, List[Tuple[Tensor, int, int]]] = {nsrt: [] for nsrt in featurizers.keys()}
        for batch_idx, states, nsrts in zip(range(batch_size), states_batch, nsrts_batch):
            for seq_idx, state, nsrt in zip(range(max_len), states, nsrts):
                grouped_data[nsrt].append((state, batch_idx, seq_idx))
                mask[batch_idx, seq_idx] = False

        # Batched execution of featurizers
        for nsrt, data in grouped_data.items():
            if not data:
                continue
            states, batch_indices, seq_indices = zip(*data)
            tokens[batch_indices, seq_indices, :] = featurizers[nsrt](np.stack(states))

        # Moving the mask to the device
        if mask.device != tokens.device:
            mask = mask.to(self._device)
        return tokens, mask

    def _init_featurizers_dataset(self, dataset: FeasibilityDataset) -> None:
        """Initializes featurizers that should be learned from that dataset
        """
        for idx in range(len(dataset)):
            (encoder_nsrts, decoder_nsrts, encoder_states, decoder_states), _ = dataset[idx]
            self._init_featurizers_datapoint(encoder_nsrts, decoder_nsrts, encoder_states, decoder_states)

    def _init_featurizers_datapoint(
        self,
        encoder_nsrts: Sequence[NSRT],
        decoder_nsrts: Sequence[NSRT],
        encoder_states: Sequence[npt.NDArray],
        decoder_states: Sequence[npt.NDArray],
    ) -> None:
        """Initializes featurizers that shoud be learned from that datapoint
        """
        for nsrt, state in zip(encoder_nsrts, encoder_states):
            self._init_featurizer(self._encoder_featurizers, self._encoder_positional_encoding.input_size, state, nsrt)
        for nsrt, state in zip(decoder_nsrts, decoder_states):
            self._init_featurizer(self._decoder_featurizers, self._decoder_positional_encoding.input_size, state, nsrt)

    def _init_featurizer(
        self,
        featurizers: Dict[NSRT, nn.Module],
        output_size: int,
        state: npt.NDArray,
        nsrt: NSRT
    ) -> None:
        """ Initializes a featurizer for a single ground nsrt.

        NOTE: The assumption is that the concatentated features of all objects that
        are passed to a given NSRT always have the same data layout and total length.
        """
        assert len(state.shape) == 1 and self._optimizer
        if nsrt not in featurizers:
            featurizer = FeasibilityFeaturizer(
                state.size,
                hidden_sizes = self._featurizer_hidden_sizes,
                feature_size = output_size,
                device = self._device
            )
            self._featurizer_count += 1

            self.add_module(f"featurizer_{self._featurizer_count}", featurizer)
            self._optimizer.add_param_group(
                {'params': featurizer.parameters(), 'lr': self._general_lr}
            )

            featurizers[nsrt] = featurizer
        else:
            featurizer = featurizers[nsrt]
        featurizer.update_range(state)