"""swepipe — turn rows of a task ledger (Feishu Bitable today; a database or an HTTP API later)
into verified, published SWE-like task image repositories.

Layers (each importable on its own):
  model     TaskRecord: the source-independent shape of one ledger row, plus the status rules
  sources   adapters that read/write a ledger: FeishuSource, JsonFileSource (template for DB/API)
  select    pure selection / de-duplication logic over TaskRecords
  ledger    bridge to the generator (xlsx2task): ledger.xlsx + records.json + overrides stub
  build     docker build + smoke test + size measurement, both architectures, N concurrent
  publish   GitHub publisher (owner / token switchable) and the publish / push flows
  config    one Config object: CLI > environment > .env > pipeline.toml > defaults
  cli       the `swepipe` command (pull / gen / build / publish / push / lint / status)
"""
__version__ = "1.0.0"
