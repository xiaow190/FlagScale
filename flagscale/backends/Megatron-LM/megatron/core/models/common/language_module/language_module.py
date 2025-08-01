# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.
import logging
import os
from typing import Optional, Tuple

import torch
from torch import Tensor

from megatron.core import parallel_state, tensor_parallel
from megatron.core.dist_checkpointing.mapping import ShardedStateDict

try:
    from megatron.core.extensions.transformer_engine import te_parallel_cross_entropy
except:
    te_parallel_cross_entropy = None
from megatron.core.fusions.fused_cross_entropy import fused_vocab_parallel_cross_entropy
from megatron.core.transformer.enums import AttnBackend
from megatron.core.transformer.module import MegatronModule
from megatron.core.transformer.transformer_config import TransformerConfig
from megatron.core.utils import make_tp_sharded_tensor_for_checkpoint


class LanguageModule(MegatronModule):
    """Base language module that has common helper functions used across GPT, BERT etc.

    Args:
        config (TransformerConfig): Input transformer config for the model
    """

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__(config=config)
        self._set_attention_backend()
        self.vp_stage = None

    # pylint: disable=line-too-long
    def _set_attention_backend(self):
        """Set attention backend

        Transformer engine works based on optout. By default all three attention backend flags are set to 1. So if the user choses a particular attention backend we set the other two to 0. If the user choses local, we set all 3 TE env variables to 0.
        """

        def check_and_set_env_variable(
            env_variable_name: str, expected_value: int, attn_type: AttnBackend
        ) -> None:
            current_value = os.getenv(env_variable_name)
            assert current_value is None or current_value == str(
                expected_value
            ), f'{env_variable_name} set to {current_value}, but expected {expected_value} for attention backend type {attn_type.name}. unset NVTE_FLASH_ATTN, NVTE_FUSED_ATTN and NVTE_UNFUSED_ATTN. Use the --attention-backend argument if you want to choose between (flash/fused/unfused/auto/local). Default is auto.'
            os.environ[env_variable_name] = str(expected_value)

        if self.config.attention_backend == AttnBackend.local:
            check_and_set_env_variable("NVTE_FLASH_ATTN", 0, AttnBackend.flash)
            check_and_set_env_variable("NVTE_FUSED_ATTN", 0, AttnBackend.flash)
            check_and_set_env_variable("NVTE_UNFUSED_ATTN", 0, AttnBackend.flash)
        elif self.config.attention_backend == AttnBackend.flash:
            check_and_set_env_variable("NVTE_FLASH_ATTN", 1, AttnBackend.flash)
            check_and_set_env_variable("NVTE_FUSED_ATTN", 0, AttnBackend.flash)
            check_and_set_env_variable("NVTE_UNFUSED_ATTN", 0, AttnBackend.flash)
        elif self.config.attention_backend == AttnBackend.fused:
            check_and_set_env_variable("NVTE_FLASH_ATTN", 0, AttnBackend.fused)
            check_and_set_env_variable("NVTE_FUSED_ATTN", 1, AttnBackend.fused)
            check_and_set_env_variable("NVTE_UNFUSED_ATTN", 0, AttnBackend.fused)
        elif self.config.attention_backend == AttnBackend.unfused:
            check_and_set_env_variable("NVTE_FLASH_ATTN", 0, AttnBackend.unfused)
            check_and_set_env_variable("NVTE_FUSED_ATTN", 0, AttnBackend.unfused)
            check_and_set_env_variable("NVTE_UNFUSED_ATTN", 1, AttnBackend.unfused)
        elif self.config.attention_backend == AttnBackend.auto:
            check_and_set_env_variable("NVTE_FLASH_ATTN", 1, AttnBackend.auto)
            check_and_set_env_variable("NVTE_FUSED_ATTN", 1, AttnBackend.auto)
            check_and_set_env_variable("NVTE_UNFUSED_ATTN", 1, AttnBackend.auto)

    def compute_language_model_loss(self, labels: Tensor, logits: Tensor) -> Tensor:
        """Computes the language model loss (Cross entropy across vocabulary)

        Args:
            labels (Tensor): The labels of dimension [batch size, seq length]
            logits (Tensor): The final logits returned by the output layer of the transformer model

        Returns:
            Tensor: Loss tensor of dimensions [batch size, sequence_length]
        """
        # [b s] => [s b]
        labels = labels.transpose(0, 1).contiguous()
        if self.config.cross_entropy_loss_fusion:
            if self.config.cross_entropy_fusion_impl == 'te':
                if te_parallel_cross_entropy is not None:
                    labels = torch.as_strided(labels, labels.size(), (labels.size()[1], 1))
                    loss = te_parallel_cross_entropy(
                        logits, labels, parallel_state.get_tensor_model_parallel_group()
                    )
                else:
                    raise RuntimeError("Trying to use a TE block when it's not present.")
            elif self.config.cross_entropy_fusion_impl == 'native':
                loss = fused_vocab_parallel_cross_entropy(
                    logits, labels, parallel_state.get_tensor_model_parallel_group()
                )
        else:
            loss = tensor_parallel.vocab_parallel_cross_entropy(logits, labels)

        # [s b] => [b, s]
        loss = loss.transpose(0, 1).contiguous()
        return loss

    def setup_embeddings_and_output_layer(self) -> None:
        """Sets up embedding layer in first stage and output layer in last stage.

        This function initalizes word embeddings in the final stage when we are
        using pipeline parallelism and sharing word embeddings, and sets up param
        attributes on the embedding and output layers.
        """

        # Set `is_embedding_or_output_parameter` attribute.
        if self.pre_process:
            self.embedding.word_embeddings.weight.is_embedding_or_output_parameter = True
        if self.post_process and self.output_layer.weight is not None:
            self.output_layer.weight.is_embedding_or_output_parameter = True

        # If share_embeddings_and_output_weights is True, we need to maintain duplicated
        # embedding weights in post processing stage. If use Multi-Token Prediction (MTP),
        # we also need to maintain duplicated embedding weights in mtp process stage.
        # So we need to copy embedding weights from pre processing stage as initial parameters
        # in these cases.
        if not self.share_embeddings_and_output_weights and not getattr(
            self.config, 'mtp_num_layers', 0
        ):
            return

        if parallel_state.get_pipeline_model_parallel_world_size() == 1:
            # Zero out wgrad if sharing embeddings between two layers on same
            # pipeline stage to make sure grad accumulation into main_grad is
            # correct and does not include garbage values (e.g., from torch.empty).
            self.shared_embedding_or_output_weight().zero_out_wgrad = True
            return

        if (
            parallel_state.is_pipeline_first_stage(ignore_virtual=False, vp_stage=self.vp_stage)
            and self.pre_process
            and not self.post_process
        ):
            self.shared_embedding_or_output_weight().shared_embedding = True

        if (self.post_process or getattr(self, 'mtp_process', False)) and not self.pre_process:
            assert not parallel_state.is_pipeline_first_stage(
                ignore_virtual=False, vp_stage=self.vp_stage
            )
            # set weights of the duplicated embedding to 0 here,
            # then copy weights from pre processing stage using all_reduce below.
            weight = self.shared_embedding_or_output_weight()
            weight.data.fill_(0)
            weight.shared = True
            weight.shared_embedding = True

        # Parameters are shared between the word embeddings layers, and the
        # heads at the end of the model. In a pipelined setup with more than
        # one stage, the initial embedding layer and the head are on different
        # workers, so we do the following:
        # 1. Create a second copy of word_embeddings on the last stage, with
        #    initial parameters of 0.0.
        # 2. Do an all-reduce between the first and last stage to ensure that
        #    the two copies of word_embeddings start off with the same
        #    parameter values.
        # 3. In the training loop, before an all-reduce between the grads of
        #    the two word_embeddings layers to ensure that every applied weight
        #    update is the same on both stages.

        # Ensure that first and last stages have the same initial parameter
        # values.
        if torch.distributed.is_initialized():
            if parallel_state.is_rank_in_embedding_group(
                ignore_virtual=False, vp_stage=self.vp_stage
            ):
                weight = self.shared_embedding_or_output_weight()
                weight.data = weight.data.cuda()
                embedding_group = parallel_state.get_embedding_group()
                if not isinstance(embedding_group, list):
                    torch.distributed.all_reduce(
                        weight.data, group=parallel_state.get_embedding_group()
                    )
                else: # for multiple embedding groups in heterogeneous mode
                    with torch.no_grad():
                        original_dtype = weight.dtype
                        if original_dtype == torch.bfloat16: # gloo backend doesn't support bfloat16
                            weight = weight.to(torch.float32)
                        if torch.distributed.get_backend(group=embedding_group[0]) == 'cpu:gloo':
                            weight.data = weight.data.cpu()
                        original_weight = weight.clone().detach().data
                        for group in embedding_group:
                            weight.data.copy_(original_weight)
                            torch.distributed.all_reduce(weight.data, group=group)
                        if original_dtype == torch.bfloat16:
                            weight = weight.to(original_dtype)
                        if weight.device == torch.device('cpu'):
                            weight.data = weight.data.cuda()

        elif not getattr(LanguageModule, "embedding_warning_printed", False):
            logging.getLogger(__name__).warning(
                "Distributed processes aren't initialized, so the output layer "
                "is not initialized with weights from the word embeddings. "
                "If you are just manipulating a model this is fine, but "
                "this needs to be handled manually. If you are training "
                "something is definitely wrong."
            )
            LanguageModule.embedding_warning_printed = True

    def shared_embedding_or_output_weight(self) -> Tensor:
        """Gets the emedding weight or output logit weights when share embedding and output weights set to True.

        Returns:
            Tensor: During pre processing it returns the input embeddings weight while during post processing it returns the final output layers weight
        """
        if self.pre_process:
            return self.embedding.word_embeddings.weight
        elif self.post_process:
            return self.output_layer.weight
        return None

    def sharded_state_dict(
        self,
        prefix: str = '',
        sharded_offsets: Tuple[Tuple[int, int, int]] = (),
        metadata: Optional[dict] = None,
    ) -> ShardedStateDict:
        """Sharded state dict implementation that handles the output layer weights tying.

        Args:
            prefix (str): Module name prefix.
            sharded_offsets (tuple): PP related offsets, expected to be empty at this module level.
            metadata (Optional[Dict]): metadata controlling sharded state dict creation.

        Returns:
            ShardedStateDict: sharded state dict for the LanguageModel
        """
        assert not sharded_offsets, "Unexpected sharded offsets"
        sharded_state_dict = super().sharded_state_dict(prefix, sharded_offsets, metadata)

        first_stage_word_emb_key = f'{prefix}embedding.word_embeddings.weight'
        output_layer_weight_key = f'{prefix}output_layer.weight'
        output_layer_bias_key = f'{prefix}output_layer.bias'

        if self.share_embeddings_and_output_weights:
            self.tie_embeddings_and_output_weights_state_dict(
                sharded_state_dict, output_layer_weight_key, first_stage_word_emb_key
            )
        elif self.post_process:
            # Make sure the output layer follows the embeddings padding logic
            sharded_state_dict[output_layer_weight_key].allow_shape_mismatch = True

        # Regardless of sharing the output weights with embeddings, we must handle the bias padding
        if self.post_process and output_layer_bias_key in sharded_state_dict:
            sharded_state_dict[output_layer_bias_key].allow_shape_mismatch = True

        return sharded_state_dict

    def tie_embeddings_and_output_weights_state_dict(
        self,
        sharded_state_dict: ShardedStateDict,
        output_layer_weight_key: str,
        first_stage_word_emb_key: str,
    ) -> None:
        """Ties the embedding and output weights in a given sharded state dict.

        Args:
            sharded_state_dict (ShardedStateDict): state dict with the weight to tie
            output_layer_weight_key (str): key of the output layer weight in the state dict.
                This entry will be replaced with a tied version
            first_stage_word_emb_key (str): this must be the same as the
                ShardedTensor.key of the first stage word embeddings.

        Returns: None, acts in-place
        """
        if not self.post_process:
            # No output layer
            assert output_layer_weight_key not in sharded_state_dict, sharded_state_dict.keys()
            return

        if self.pre_process:
            # Output layer is equivalent to the embedding already
            return

        # If use Multi-Token Prediction (MTP), we need maintain both embedding layer and output
        # layer in mtp process stage. In this case, if share_embeddings_and_output_weights is True,
        # the shared weights will be stored in embedding layer, and output layer will not have
        # any weight.
        if getattr(self, 'mtp_process', False):
            # No output layer
            assert output_layer_weight_key not in sharded_state_dict, sharded_state_dict.keys()
            return

        # Replace the default output layer with a one sharing the weights with the embedding
        del sharded_state_dict[output_layer_weight_key]
        tensor = self.shared_embedding_or_output_weight()
        last_stage_word_emb_replica_id = (
            1,  # copy of first stage embedding
            0,
            parallel_state.get_data_parallel_rank(with_context_parallel=True),
        )

        sharded_state_dict[output_layer_weight_key] = make_tp_sharded_tensor_for_checkpoint(
            tensor=tensor,
            key=first_stage_word_emb_key,
            replica_id=last_stage_word_emb_replica_id,
            allow_shape_mismatch=True,
        )
