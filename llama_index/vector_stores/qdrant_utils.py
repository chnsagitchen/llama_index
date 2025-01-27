from typing import Any, Callable, List, Protocol, Tuple, runtime_checkable

from llama_index.vector_stores.types import VectorStoreQueryResult

SparseEncoderCallable = Callable[[List[str]], Tuple[List[List[int]], List[List[float]]]]


@runtime_checkable
class HybridFusionCallable(Protocol):
    """Hybrid fusion callable protocol."""

    def __call__(
        self,
        dense_result: VectorStoreQueryResult,
        sparse_result: VectorStoreQueryResult,
        **kwargs: Any,
    ) -> VectorStoreQueryResult:
        """Hybrid fusion callable."""
        ...


def default_sparse_encoder(model_id: str) -> SparseEncoderCallable:
    try:
        import torch
        from transformers import AutoModelForMaskedLM, AutoTokenizer
    except ImportError:
        raise ImportError(
            "Could not import transformers library. "
            'Please install transformers with `pip install "transformers[torch]"`'
        )

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForMaskedLM.from_pretrained(model_id)
    if torch.cuda.is_available():
        model = model.to("cuda")

    def compute_vectors(texts: List[str]) -> Tuple[List[List[int]], List[List[float]]]:
        """
        Computes vectors from logits and attention mask using ReLU, log, and max operations.
        """
        # TODO: compute sparse vectors in batches if max length is exceeded
        tokens = tokenizer(
            texts, truncation=True, padding=True, max_length=512, return_tensors="pt"
        )
        if torch.cuda.is_available():
            tokens = tokens.to("cuda")

        output = model(**tokens)
        logits, attention_mask = output.logits, tokens.attention_mask
        relu_log = torch.log(1 + torch.relu(logits))
        weighted_log = relu_log * attention_mask.unsqueeze(-1)
        tvecs, _ = torch.max(weighted_log, dim=1)

        # extract the vectors that are non-zero and their indices
        indices = []
        vecs = []
        for batch in tvecs:
            indices.append(batch.nonzero(as_tuple=True)[0].tolist())
            vecs.append(batch[indices[-1]].tolist())

        return indices, vecs

    return compute_vectors


def relative_score_fusion(
    dense_result: VectorStoreQueryResult,
    sparse_result: VectorStoreQueryResult,
    alpha: float = 0.5,
    top_k: int = 2,
) -> VectorStoreQueryResult:
    """
    Fuse dense and sparse results using relative score fusion.
    """
    # sanity check
    assert dense_result.nodes is not None
    assert dense_result.similarities is not None
    assert sparse_result.nodes is not None
    assert sparse_result.similarities is not None

    # deconstruct results
    sparse_result_tuples = list(zip(sparse_result.similarities, sparse_result.nodes))
    sparse_result_tuples.sort(key=lambda x: x[0], reverse=True)

    dense_result_tuples = list(zip(dense_result.similarities, dense_result.nodes))
    dense_result_tuples.sort(key=lambda x: x[0], reverse=True)

    # track nodes in both results
    all_nodes_dict = {x.node_id: x for x in dense_result.nodes}
    for node in sparse_result.nodes:
        if node.node_id not in all_nodes_dict:
            all_nodes_dict[node.node_id] = node

    # normalize sparse similarities from 0 to 1
    sparse_similarities = [x[0] for x in sparse_result_tuples]

    sparse_per_node = {}
    if len(sparse_similarities) > 0:
        max_sparse_sim = max(sparse_similarities)
        min_sparse_sim = min(sparse_similarities)

        # avoid division by zero
        if max_sparse_sim == min_sparse_sim:
            sparse_similarities = [max_sparse_sim] * len(sparse_similarities)
        else:
            sparse_similarities = [
                (x - min_sparse_sim) / (max_sparse_sim - min_sparse_sim)
                for x in sparse_similarities
            ]

        sparse_per_node = {
            sparse_result_tuples[i][1].node_id: x
            for i, x in enumerate(sparse_similarities)
        }

    # normalize dense similarities from 0 to 1
    dense_similarities = [x[0] for x in dense_result_tuples]

    dense_per_node = {}
    if len(dense_similarities) > 0:
        max_dense_sim = max(dense_similarities)
        min_dense_sim = min(dense_similarities)

        # avoid division by zero
        if max_dense_sim == min_dense_sim:
            dense_similarities = [max_dense_sim] * len(dense_similarities)
        else:
            dense_similarities = [
                (x - min_dense_sim) / (max_dense_sim - min_dense_sim)
                for x in dense_similarities
            ]

        dense_per_node = {
            dense_result_tuples[i][1].node_id: x
            for i, x in enumerate(dense_similarities)
        }

    # fuse the scores
    fused_similarities = []
    for node_id in all_nodes_dict:
        sparse_sim = sparse_per_node.get(node_id, 0)
        dense_sim = dense_per_node.get(node_id, 0)
        fused_sim = alpha * (sparse_sim + dense_sim)
        fused_similarities.append((fused_sim, all_nodes_dict[node_id]))

    fused_similarities.sort(key=lambda x: x[0], reverse=True)
    fused_similarities = fused_similarities[:top_k]

    # create final response object
    return VectorStoreQueryResult(
        nodes=[x[1] for x in fused_similarities],
        similarities=[x[0] for x in fused_similarities],
        ids=[x[1].node_id for x in fused_similarities],
    )
