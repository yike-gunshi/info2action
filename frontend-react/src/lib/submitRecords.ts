export function submitRecordsStorageKey(userId: string | null | undefined): string {
  return `submit_records:${userId || 'anon'}`
}
