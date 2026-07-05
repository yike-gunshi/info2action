#!/usr/bin/env node
// Browser/API smoke checks for the 2026-04-24 review closure pass.
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

function parseArgs(argv) {
  const args = {}
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i]
    if (!key.startsWith('--')) continue
    args[key.slice(2)] = argv[i + 1]
    i += 1
  }
  for (const required of ['base', 'meta', 'out', 'screenshot']) {
    if (!args[required]) {
      throw new Error(`Missing --${required}`)
    }
  }
  return args
}

class Recorder {
  constructor() {
    this.checks = []
  }

  check(name, ok, detail = null) {
    this.checks.push({ name, ok: Boolean(ok), detail })
  }

  expectStatus(name, result, status) {
    const expected = Array.isArray(status) ? status : [status]
    this.check(name, expected.includes(result.status), result)
  }

  get failed() {
    return this.checks.filter((check) => !check.ok)
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  const baseUrl = args.base.replace(/\/$/, '')
  const meta = JSON.parse(fs.readFileSync(args.meta, 'utf8'))
  const outPath = path.resolve(args.out)
  const screenshotPath = path.resolve(args.screenshot)
  fs.mkdirSync(path.dirname(outPath), { recursive: true })
  fs.mkdirSync(path.dirname(screenshotPath), { recursive: true })

  const recorder = new Recorder()
  const consoleErrors = []
  const pageErrors = []

  function writeReport() {
    const failed = recorder.failed
    const report = {
      generated_at: new Date().toISOString(),
      base_url: baseUrl,
      summary: {
        passed: recorder.checks.length - failed.length,
        failed: failed.length,
        total: recorder.checks.length,
      },
      checks: recorder.checks,
      console_errors: consoleErrors,
      page_errors: pageErrors,
      screenshots: [screenshotPath],
    }
    fs.writeFileSync(outPath, JSON.stringify(report, null, 2))
  }

  const browser = await chromium.launch({ headless: process.env.HEADLESS !== '0' })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    baseURL: baseUrl,
  })
  const page = await context.newPage()
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text())
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  async function api(pathname, method = 'GET', body = null, headers = null) {
    return page.evaluate(async ({ pathname, method, body, headers }) => {
      const init = { method, credentials: 'same-origin', headers: headers || {} }
      if (body !== null && body !== undefined) {
        init.headers = { ...init.headers, 'Content-Type': 'application/json' }
        init.body = JSON.stringify(body)
      }
      const res = await fetch(pathname, init)
      const text = await res.text()
      let data = text
      try {
        data = JSON.parse(text)
      } catch {}
      return { status: res.status, ok: res.ok, data, text }
    }, { pathname, method, body, headers })
  }

  try {
    await page.goto(`${baseUrl}/`, { waitUntil: 'networkidle' })
    recorder.check('anonymous shell renders', await page.locator('text=info2act').first().isVisible())

    recorder.expectStatus('anonymous config read denied', await api('/api/config'), 401)
    recorder.expectStatus('anonymous config write denied', await api('/api/config', 'POST', { x: 1 }), 401)
    recorder.expectStatus('anonymous credential sync denied', await api('/api/health/sync-credentials', 'POST', {}), 401)
    recorder.expectStatus('anonymous feedback write denied', await api('/api/feedback', 'POST', { x: 1 }), 401)
    recorder.expectStatus('anonymous feedback text denied', await api('/api/feedback'), 401)
    recorder.expectStatus('anonymous manual feed detail hidden', await api(`/api/feed/item/${meta.items.alice_manual}`), 404)
    recorder.expectStatus('encoded traversal under assets blocked', await api('/assets/..%2F..%2Fdata%2Ffeed.db'), [400, 404])

    const bobId = meta.users.bob.id
    const aliceId = meta.users.alice.id
    await page.evaluate(({ aliceId, bobId }) => {
      localStorage.setItem(`submit_records:${aliceId}`, JSON.stringify([{
        url: 'https://example.com/alice-old',
        title: 'Alice Previous Browser URL',
        status: 'done',
        submittedAt: new Date().toISOString(),
        itemId: 'manual-alice-review',
      }]))
      localStorage.setItem(`submit_records:${bobId}`, JSON.stringify([{
        url: 'https://example.com/bob-current',
        title: 'Bob Current Browser URL',
        status: 'done',
        submittedAt: new Date().toISOString(),
        itemId: 'public-review-item',
      }]))
    }, { aliceId, bobId })

    await page.goto(`${baseUrl}/#login`, { waitUntil: 'networkidle' })
    await page.locator('input[autocomplete="username"]').fill(meta.users.bob.email)
    await page.locator('input[autocomplete="current-password"]').fill(meta.password)
    await page.locator('button[type="submit"]').click()
    await page.waitForFunction(() => window.location.hash === '', null, { timeout: 5000 })
    await page.waitForLoadState('networkidle')
    const me = await api('/api/auth/me')
    recorder.check('regular user login succeeds', me.status === 200 && me.data.email === meta.users.bob.email, me)

    await page.locator('button[aria-label="提交链接"]').click()
    const panelText = await page.locator('body').innerText({ timeout: 3000 })
    recorder.check("SubmitPanel shows current user's records", panelText.includes('Bob Current Browser URL'))
    recorder.check("SubmitPanel hides previous user's records", !panelText.includes('Alice Previous Browser URL'))
    await page.screenshot({ path: screenshotPath, fullPage: true })

    const aliceAction = meta.actions.alice
    const aliceInterest = meta.interests.alice
    const adminGated = [
      ['regular user cannot start fetch', '/api/fetch', 'POST', {}],
      ['regular user cannot quick fetch', '/api/fetch/quick', 'POST', { hours: 1 }],
      ['regular user cannot generate briefing', '/api/briefing/generate', 'POST', {}],
      ['regular user cannot auto-generate actions', '/api/actions/auto-generate', 'POST', {}],
      ['regular user cannot generate action from item', '/api/actions/generate-from-item', 'POST', { item_id: meta.items.public }],
      ['regular user cannot stream action logs', `/api/actions/${aliceAction}/stream`, 'GET', null],
      ['regular user cannot confirm another action', `/api/actions/${aliceAction}/confirm`, 'POST', { tool: 'codex' }],
      ['regular user cannot execute another action', `/api/actions/${aliceAction}/execute`, 'POST', { tool: 'codex' }],
      ['regular user cannot retarget another action', `/api/actions/${aliceAction}`, 'PATCH', { title: 'Retargeted' }],
      ['regular user cannot inspect CLI', '/api/cli/status', 'GET', null],
      ['regular user cannot read project dirs', '/api/settings/project-dirs', 'GET', null],
      ['regular user cannot rewrite project dirs', '/api/settings/project-dirs', 'POST', { project_dirs: ['/tmp'] }],
      ['regular user cannot read token', '/api/token', 'GET', null],
      ['regular user cannot read config', '/api/config', 'GET', null],
      ['regular user cannot scan interest', `/api/interests/${aliceInterest}/scan`, 'POST', {}],
      ['regular user cannot list ttyd sessions', '/api/ttyd/sessions', 'GET', null],
    ]
    for (const [name, pathname, method, body] of adminGated) {
      recorder.expectStatus(name, await api(pathname, method, body), 403)
    }

    const actions = await api('/api/actions')
    const titles = (actions.data.actions || []).map((action) => action.title)
    recorder.check('regular user sees own action', titles.includes('Bob Visible Action'), titles)
    recorder.check("regular user cannot list another user's action", !titles.includes('Alice Secret Action'), titles)
    recorder.expectStatus('regular user cannot read another action', await api(`/api/actions/${aliceAction}`), 404)
    recorder.expectStatus('regular user cannot update another action priority', await api(`/api/actions/${aliceAction}/priority`, 'PATCH', { priority: 'high' }), 404)
    recorder.expectStatus('regular user cannot delete another action', await api(`/api/actions/${aliceAction}`, 'DELETE'), 404)

    const interests = await api('/api/interests')
    const interestNames = (interests.data.interests || []).map((interest) => interest.name)
    recorder.check('regular user sees own interest', interestNames.includes('Bob Visible Interest'), interestNames)
    recorder.check("regular user cannot list another user's interest", !interestNames.includes('Alice Secret Interest'), interestNames)
    recorder.expectStatus('regular user cannot edit another interest', await api(`/api/interests/${aliceInterest}`, 'POST', { name: 'Leaked' }), 404)

    const manualItem = meta.items.alice_manual
    recorder.expectStatus('regular user cannot read another ASR state', await api(`/api/items/${manualItem}/asr`), 404)
    recorder.expectStatus('regular user cannot stream another ASR state', await api(`/api/items/${manualItem}/asr/stream`), 404)
    recorder.expectStatus('regular user cannot trigger another ASR', await api(`/api/items/${manualItem}/asr`, 'POST', {}), 404)
    recorder.expectStatus('regular user cannot translate another ASR', await api(`/api/items/${manualItem}/asr/translate`, 'POST', {}), 404)
    const history = await api('/api/submit-history')
    recorder.check("submit history excludes another user's manual item", !history.text.includes('Alice Private Manual'), history)
    const exportCsv = await api('/api/export')
    recorder.check("export excludes another user's manual item", !exportCsv.text.includes('Alice Private Manual'), exportCsv)

    await api('/api/auth/logout', 'POST', {})
    const adminLogin = await api('/api/auth/login', 'POST', { login: meta.users.admin.email, password: meta.password })
    recorder.expectStatus('admin API login succeeds', adminLogin, 200)
    recorder.expectStatus('admin can read config', await api('/api/config'), 200)
    recorder.expectStatus('admin can read legacy token', await api('/api/token'), 200)
    const adminActions = await api('/api/actions')
    const adminTitles = (adminActions.data.actions || []).map((action) => action.title)
    recorder.check('admin can list all actions', adminTitles.includes('Alice Secret Action') && adminTitles.includes('Bob Visible Action'), adminTitles)
    recorder.expectStatus('admin can read manual feed detail', await api(`/api/feed/item/${manualItem}`), 200)
  } catch (err) {
    recorder.check('browser flow crashed', false, String(err?.stack || err))
  } finally {
    await browser.close()
  }

  const unexpectedConsoleErrors = consoleErrors.filter(
    (message) => !message.startsWith('Failed to load resource: the server responded with a status of '),
  )
  if (unexpectedConsoleErrors.length) {
    recorder.check('browser console has no unexpected errors', false, unexpectedConsoleErrors)
  }
  if (pageErrors.length) recorder.check('browser page has no uncaught errors', false, pageErrors)
  writeReport()

  if (recorder.failed.length) {
    console.error(`QA failed: ${recorder.failed.map((check) => check.name).join(', ')}`)
    process.exitCode = 1
    return
  }
  console.log(`QA passed: ${recorder.checks.length} checks`)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
