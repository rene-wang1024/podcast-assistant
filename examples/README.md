# 示例：EP00（虚构样例）

本目录是一段**虚构**的三人对谈，用来演示目录结构和试跑流程，不对应任何真实节目或案件。

```bash
cp -r examples/EP00 episodes/EP00
PIPELINE_PROVIDER_OVERRIDE=mock python -m pipeline run all --episode EP00
```

`PIPELINE_PROVIDER_OVERRIDE=mock` 会让所有工作流走 mock 提供方，不调用任何模型、不花钱，只检查流程能跑通。去掉它就会按 `config/pipeline.yaml` 调用真实模型。
