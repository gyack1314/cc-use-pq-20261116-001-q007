街区诉求回应与治理分析服务。依赖需在离线运行前按 requirements.txt 预装。
启动 API：uvicorn civic_flow.api:app --host 127.0.0.1 --port 8000
运行测试：python -B -m unittest discover -s tests -v
源码检查：python -B -m civic_flow.check_source
本地验收：python -B -m civic_flow.demo

治理问题与诉求的关系
--------------------
- 每份来源独立的诉求（cases）始终单独保留；多位居民就同一盏路灯从热线、
  窗口等渠道报修，也只是被“关联”到同一个治理问题（problems），不会被合并
  成一条数据，也不会被当成重复数据丢弃。
- 相似内容只作为关联线索（association_suggestions）。线索必须经
  POST /associations/{id}/confirm 确认后才开问题或并入指定问题；
  未确认的线索不影响任何治理问题量。
- 相关接口：
  POST /problems、GET /problems、GET /problems/{id}?as_of=、GET /problems/{id}/cases、
  POST /problems/{id}/cases、POST /problems/{id}/split、POST /problems/{id}/merge、
  POST /merges/{id}/undo、POST /associations、POST /associations/scan、
  POST /associations/{id}/confirm|reject、GET /associations。
- 合并、拆分、撤销合并都是追加事件：诉求自身的状态、回应与结案记录不变；
  拆分只移动仍未履行完义务的诉求，已办完的诉求保留原记录与原归属
  （move_completed=true 可显式覆盖）。回应始终绑定具体 case_id，与拆分
  并发也不会套错居民。
- GET /metrics 同时给出原始诉求量（raw_cases）、治理问题量
  （governance_problems）与未履行义务（unfulfilled_response/_resolution），
  并可传 watermark 按关联发生前后的日志版本重算。
- 报表快照保存 watermark、as_of 与 grouping_basis（当时的分组依据），
  GET /reports/{key}/verify 与 GET /reports/{key}/basis 可复核。
- 相同来源、相同 external_id 的批次重送保持幂等；不同来源的相似内容
  只生成线索。
