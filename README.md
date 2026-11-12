# 跨境发运EDIFACT交换的层级来源采集

这是商品与服务履约资料的独立Python工具。`data/`保存合成报文、导出文件或解码服务响应，`source_data.io`读取清单并核对原文件身份，`source_data.edifact`按字节块扫描语法3，`source_data.envelope`核对包络，`source_data.parse`解析DESADV层级，`source_data.ingest`在整交换通过后提交记录，`source_data.domain`提供原始资料概况，`source_data.replay`按记录返回响应，`source_data.cli`显示JSON摘要或采集结果。字段与来源格式见[资料约定](docs/data-contract.md)。不接入真实商家、支付账户或在线平台。

Python 3.11及以上，无第三方Python依赖。在项目根目录使用：

```sh
python -m unittest discover -s tests -v
python -m compileall -q source_data tests
python -m source_data.cli inspect data/normal.json
python -m source_data.cli collect data/normal.json
```

`collect`输出可见记录或隔离记录；成功记录仍标记为待核对，不用于直接确认收货。

`inspect`仅读取清单、原始字节身份和基本配置，不覆盖资料，也不提交交易记录。`invalid-config.json`是配置反例，其他清单有不同的源边界，边界报文可以保持合法清单但源内容不完整。资料中的姓名、编号、订单和站点均为虚构。

Dockerfile固定Linux基础镜像。`docker build -t commerce-source .`构建，`docker run --rm commerce-source`读取正常清单；`docker run --rm commerce-source python -m unittest discover -s tests -v`运行已有检查。运行资料均在本地，不需要账号、安装脚本或生产系统。
