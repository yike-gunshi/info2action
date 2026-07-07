-- v21.0 action-revival (D3): 每用户 Discord 派发目标频道。
--
-- Why: 派发从"全局 config.json forum_channel_id"改为每用户可配置自己的 channel。
-- forum channel → 建 thread;普通 text channel → 发消息。全局配置仅作 admin fallback。
--
-- 使用方:src/routes/user.py 设置读写;src/routes/actions.py dispatch 读取。

ALTER TABLE remote_poc.users ADD COLUMN IF NOT EXISTS discord_channel_id TEXT;
