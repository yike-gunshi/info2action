import { apiFetch } from './_client'

// ── User Settings ──

export async function getUserSettings(): Promise<{
  discord_bot_token: string | null
  has_discord_token: boolean
  discord_channel_id?: string
}> {
  return apiFetch('/api/user/settings')
}

export async function updateUserSettings(data: {
  discord_bot_token?: string
  discord_channel_id?: string
}): Promise<{ ok: boolean }> {
  return apiFetch('/api/user/settings', {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}
// ── User Profile ──

export interface UserProfile {
  role: string | null
  interests: string[]
  tools: string[]
  manifest: string | null
}

export async function getUserProfile(): Promise<{ profile: UserProfile | null; onboarding_completed: boolean }> {
  return apiFetch('/api/user/profile')
}

export async function updateUserProfile(data: {
  role?: string
  interests?: string[]
  tools?: string[]
  manifest?: string
  onboarding_completed?: boolean
}): Promise<{ ok: boolean; profile: UserProfile }> {
  return apiFetch('/api/user/profile', {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}
