-- Expand the remote asset bucket MIME allow-list for historical image/media
-- migration. Idempotent across staging and production.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'info2action-assets',
  'info2action-assets',
  false,
  null,
  array[
    'image/jpeg',
    'image/png',
    'image/webp',
    'image/gif',
    'image/avif',
    'image/svg+xml',
    'text/html',
    'application/octet-stream',
    'audio/mpeg',
    'audio/mp3',
    'audio/mp4',
    'audio/wav',
    'audio/x-wav',
    'audio/aac',
    'audio/ogg',
    'video/mp4',
    'video/webm',
    'video/quicktime'
  ]::text[]
)
on conflict (id) do update set
  allowed_mime_types = excluded.allowed_mime_types;
