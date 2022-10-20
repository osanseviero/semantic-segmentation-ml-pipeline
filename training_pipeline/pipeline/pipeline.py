from typing import Any, Dict, List, Optional, Text

import tensorflow_model_analysis as tfma
from ml_metadata.proto import metadata_store_pb2
from tfx import v1 as tfx
from tfx.components import (
    Evaluator,
    ImportExampleGen,
    SchemaGen,
    StatisticsGen,
    Transform,
)
from tfx.dsl.components.common import resolver
from tfx.dsl.experimental.latest_blessed_model_resolver import LatestBlessedModelResolver
from tfx.extensions.google_cloud_ai_platform.pusher.component import (
    Pusher as VertexPusher,
)
from tfx.extensions.google_cloud_ai_platform.trainer.component import (
    Trainer as VertexTrainer,
)
from tfx.orchestration import pipeline
from tfx.proto import example_gen_pb2, trainer_pb2
from tfx.types import Channel
from tfx.types.standard_artifacts import Model, ModelBlessing

from pipeline.components.HFPusher.component import HFPusher


def create_pipeline(
    pipeline_name: Text,
    pipeline_root: Text,
    data_path: Text,
    modules: Dict[Text, Text],
    train_args: trainer_pb2.TrainArgs,
    eval_args: trainer_pb2.EvalArgs,
    eval_configs: tfma.EvalConfig,
    metadata_connection_config: Optional[metadata_store_pb2.ConnectionConfig] = None,
    ai_platform_training_args: Optional[Dict[Text, Text]] = None,
    ai_platform_serving_args: Optional[Dict[Text, Any]] = None,
    example_gen_beam_args: Optional[List] = None,
    transform_beam_args: Optional[List] = None,
    hf_pusher_args: Optional[Dict[Text, Any]] = None,
) -> tfx.dsl.Pipeline:
    components = []

    # Data splitting config.
    input_config = example_gen_pb2.Input(
        splits=[
            example_gen_pb2.Input.Split(name="train", pattern="train-*.tfrec"),
            example_gen_pb2.Input.Split(name="eval", pattern="val-*.tfrec"),
        ]
    )

    # Data input (pipeline starts here).
    example_gen = ImportExampleGen(input_base=data_path, input_config=input_config)
    if example_gen_beam_args is not None:
        example_gen.with_beam_pipeline_args(example_gen_beam_args)
    components.append(example_gen)

    # Generate stats from the data. Useful for preprocessing, post-processing,
    # anomaly detection, etc.
    statistics_gen = StatisticsGen(examples=example_gen.outputs["examples"])
    components.append(statistics_gen)

    schema_gen = SchemaGen(statistics=statistics_gen.outputs["statistics"])
    components.append(schema_gen)

    # Apply any preprocessing. Transformations get saved as a graph in a SavedModel.
    transform = Transform(
        examples=example_gen.outputs["examples"],
        schema=schema_gen.outputs["schema"],
        preprocessing_fn=modules["preprocessing_fn"],
    )
    if transform_beam_args is not None:
        transform.with_beam_pipeline_args(transform_beam_args)
    components.append(transform)

    # Training.
    trainer_args = {
        "run_fn": modules["training_fn"],
        "transformed_examples": transform.outputs["transformed_examples"],
        "transform_graph": transform.outputs["transform_graph"],
        "schema": schema_gen.outputs["schema"],
        "train_args": train_args,
        "eval_args": eval_args,
        "custom_config": ai_platform_training_args,
    }
    trainer = VertexTrainer(**trainer_args)
    components.append(trainer)

    # Resolver component - did we do better than the previous model?
    model_resolver = resolver.Resolver(
        strategy_class=LatestBlessedModelResolver,
        model=Channel(type=Model),
        model_blessing=Channel(type=ModelBlessing),
    ).with_id("latest_blessed_model_resolver")
    components.append(model_resolver)

    # Evaluate the model.
    evaluator = Evaluator(
        examples=example_gen.outputs["examples"],
        model=trainer.outputs["model"],
        baseline_model=model_resolver.outputs["model"],
        eval_config=eval_configs,
    )
    components.append(evaluator)

    # Based on blessing status, push the model to prod (deployment stage.)
    pusher_args = {
        "model": trainer.outputs["model"],
        "model_blessing": evaluator.outputs["blessing"],
        "custom_config": ai_platform_serving_args,
    }
    pusher = VertexPusher(**pusher_args)  # pylint: disable=unused-variable
    components.append(pusher)

    # Push the blesses model to HF hub and deploy a demo app on Hugging Face
    # Spaces.
    hf_pusher_args["model"] = trainer.outputs["model"]
    hf_pusher_args["model_blessing"] = evaluator.outputs["blessing"]
    hf_pusher = HFPusher(**hf_pusher_args)
    components.append(hf_pusher)

    return pipeline.Pipeline(
        pipeline_name=pipeline_name,
        pipeline_root=pipeline_root,
        components=components,
        enable_cache=True,
        metadata_connection_config=metadata_connection_config,
    )
