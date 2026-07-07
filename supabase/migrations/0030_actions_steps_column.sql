-- v21.0 action-revival (Q6): actions 增加 steps 列。
--
-- Why: 生成 schema 三字段分离 —— reason(为什么)/steps(人看的结构化行动点)/
-- prompt(交给本地 Agent 的自包含可执行指令)。steps 存 JSON 文本(字符串数组);
-- 行动详情 read model 优先读 steps,无则回退拆 prompt(旧数据不回归)。
--
-- 使用方:src/remote_db.py create_action_remote 写入;action_detail_read_model
-- extract_action_steps 读取。

ALTER TABLE remote_poc.actions ADD COLUMN IF NOT EXISTS steps TEXT;
