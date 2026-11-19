import assert from 'node:assert/strict'
import { test } from 'node:test'
import { chromium } from 'playwright-core'
import { backend } from './harness.mjs'

const timeout = 10_000
const visible = locator => locator.waitFor({ state: 'visible', timeout })
const utterances = page => page.locator('.utterance > p')
const room = page => page.getByRole('main', { name: '演播厅' })
const phase = (page, text) => room(page).locator('.tag').getByText(text, { exact: true })
const finding = (page, topic) => page.locator('.finding-group.consensus').getByText(topic + '应该先试点', { exact: true })
const utteranceAt = (page, topic, ordinal, host) => utterances(page).getByText(
  new RegExp(`^${ordinal}．${topic}${host ? '主持人发言' : '建议'}`),
)
async function noHorizontalOverflow(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), page.viewportSize().width)
}

async function setup(t) {
  const browser = await chromium.launch({
    executablePath: process.env.PANEL_E2E_CHROMIUM || undefined,
    args: ['--no-sandbox'],
  })
  t.after(() => browser.close())
  const api = backend(t)

  async function newPage(viewport = { width: 1280, height: 800 }) {
    const context = await browser.newContext({ viewport })
    await api.route(context)
    const page = await context.newPage()
    return page
  }
  return { ...api, newPage }
}

async function roster(page, origin, topic) {
  await page.goto(origin)
  await page.getByLabel('讨论话题').fill(topic)
  assert.equal(await page.getByLabel('专家人数').inputValue(), '4')
  await page.getByRole('button', { name: '生成阵容' }).click()
  const confirm = page.getByRole('button', { name: '确认并开始' })
  await visible(confirm)
  return confirm
}

async function start(page, api, topic) {
  const confirm = await roster(page, api.origin, topic)
  await confirm.click()
  await visible(room(page))
  await visible(phase(page, '进行中'))
  await visible(utteranceAt(page, topic, 1, true))
}

async function finish(page, topic) {
  await visible(phase(page, '已结束'))
  await visible(page.getByRole('heading', { name: '主持人总结' }))
  const summary = await page.locator('.summary p').innerText()
  assert.equal(summary, topic + '的实施方式存在分歧，仍待确定。')
}

test('AC-01 首页创建默认四专家、核对阵容、确认开场', { timeout: 35_000 }, async t => {
  const api = await setup(t)
  const page = await api.newPage()
  const topic = '默认阵容'
  const confirm = await roster(page, api.origin, topic)
  const groups = page.locator('.roster-group')
  const host = groups.filter({ has: page.getByRole('heading', { name: '主持人', exact: true }) })
  const experts = groups.filter({ has: page.getByRole('heading', { name: '专家', exact: true }) })
  assert.equal(await host.locator('.person').count(), 1)
  assert.equal(await experts.locator('.person').count(), 4)
  for (let position = 0; position < 5; position += 1) {
    const person = page.locator('.roster .person').nth(position)
    await visible(person.getByRole('heading', { name: topic + '嘉宾' + position }))
    await visible(person.getByText(position === 0 ? '圆桌主持人' : '议题专家'))
    await visible(person.getByText('关注本场试点'))
    assert.match(await person.getAttribute('style'), /--person-color:\s*#[\da-f]{6}/i)
  }
  assert.equal(await room(page).count(), 0)
  const url = page.url()
  await confirm.click()
  await visible(room(page))
  await visible(utteranceAt(page, topic, 1, true))
  assert.equal(page.url(), url)
  await noHorizontalOverflow(page)
})

test('AC-05/06/07 开场、专家状态、结束前共识与结束总结', { timeout: 35_000 }, async t => {
  const api = await setup(t)
  const page = await api.newPage({ width: 1920, height: 1080 })
  const topic = '直播观察'
  const streamReads = []
  page.on('response', response => {
    if (!response.url().includes('/events?') || !response.ok()) return
    streamReads.push(response.text().then(body => body.split('\n\n').flatMap(frame => {
      const lines = frame.split('\n')
      if (!lines.includes('event: participant.status')) return []
      return [JSON.parse(lines.find(line => line.startsWith('data: ')).slice(6))]
    })).catch(() => []))
  })
  await page.addInitScript(() => {
    window.firstExpertStatuses = []
    new MutationObserver(() => {
      const status = document.querySelector('.expert-list .person .expert-state')?.textContent?.trim()
      if (status && window.firstExpertStatuses.at(-1) !== status) window.firstExpertStatuses.push(status)
    }).observe(document, { subtree: true, childList: true, characterData: true })
  })
  await start(page, api, topic)
  await visible(page.locator('.expert-list .person').first().getByText('待机', { exact: true }))
  await api.release(topic, 0)
  await visible(utteranceAt(page, topic, 2, false))
  const expert = page.locator('.expert-list .person').first()
  await visible(expert.getByText(topic + '试点安排', { exact: true }))
  await visible(finding(page, topic))
  await visible(phase(page, '进行中'))
  assert.equal(await page.locator('.summary').count(), 0)
  await api.release(topic, 2)
  await finish(page, topic)
  const frames = (await Promise.all(streamReads)).flat()
  const firstExpert = frames[0]?.participant_id
  assert.deepEqual(frames.filter(frame => frame.participant_id === firstExpert).map(frame => frame.public_status),
    ['preparing', 'speaking', 'idle'], 'SSE 应包含第一名专家的完整状态帧')
  assert.deepEqual(await page.evaluate(() => window.firstExpertStatuses), ['待机', '准备发言', '发言中', '待机'],
    '专家小窗的可观察状态应该按顺序经历准备发言与发言中')
})

test('AC-02 独立访客会话从首页接续历史和后续发言', { timeout: 35_000 }, async t => {
  const api = await setup(t)
  const topic = '已有讨论'
  const host = await api.newPage()
  const viewer = await api.newPage()
  assert.notEqual(host.context(), viewer.context())
  await start(host, api, topic)
  const writes = []
  viewer.on('request', request => {
    if (request.url().includes('/api/') && request.method() !== 'GET') writes.push(request.url())
  })
  await viewer.goto(api.origin)
  await viewer.locator('.session').filter({ hasText: topic }).getByRole('link', { name: '进入演播厅' }).click()
  await visible(utteranceAt(viewer, topic, 1, true))
  assert.equal(await utterances(viewer).count(), 1)
  await api.release(topic, 0)
  await visible(utteranceAt(viewer, topic, 2, false))
  await visible(phase(viewer, '进行中'))
  assert.deepEqual(writes, [])
})

test('AC-08 两场并行、刷新恢复历史并防止重复发言', { timeout: 35_000 }, async t => {
  const api = await setup(t)
  const viewport = { width: 1920, height: 1080 }
  const pages = await Promise.all([api.newPage(viewport), api.newPage(viewport)])
  const topics = ['远程办公', '城市交通']
  await Promise.all(pages.map((page, i) => start(page, api, topics[i])))
  await Promise.all(pages.map((page, i) => api.release(topics[i], 0)))
  await Promise.all(pages.map((page, i) => visible(finding(page, topics[i]))))
  await Promise.all(pages.map(page => visible(phase(page, '进行中'))))
  const history = await utterances(pages[0]).allTextContents()
  assert.equal(history.length, 3)
  await pages[0].reload()
  await visible(finding(pages[0], topics[0]))
  assert.deepEqual(await utterances(pages[0]).allTextContents(), history)
  await Promise.all(pages.map((page, i) => api.release(topics[i], 2)))
  await Promise.all(pages.map((page, i) => finish(page, topics[i])))

  for (const [i, page] of pages.entries()) {
    const topic = topics[i]
    await visible(page.getByRole('heading', { name: topic, exact: true }))
    await visible(page.locator('.host').getByText(topic + '嘉宾0'))
    const lines = await utterances(page).allTextContents()
    assert.equal(lines.length, 7)
    assert.equal(new Set(lines).size, 7)
    assert(lines.every(line => line.includes(topic) && !line.includes(topics[1 - i])))
    await visible(page.locator('.finding-group.disagreement').getByText(topic + '应该先试点', { exact: true }))
    assert(!(await room(page).innerText()).includes(topics[1 - i]))
  }
})

test('AC-09 390×844 可创建、确认并切换三个演播厅区域', { timeout: 35_000 }, async t => {
  const api = await setup(t)
  const page = await api.newPage({ width: 390, height: 844 })
  const topic = '窄屏操作'
  await page.goto(api.origin)
  await noHorizontalOverflow(page)
  const confirm = await roster(page, api.origin, topic)
  await noHorizontalOverflow(page)
  await confirm.click()
  await visible(room(page))
  await visible(utteranceAt(page, topic, 1, true))
  const tabs = page.getByRole('navigation', { name: '演播厅面板' })
  await tabs.getByRole('button', { name: '专家席' }).click()
  await visible(page.locator('.experts').getByText('待机', { exact: true }).first())
  await api.release(topic, 0)
  await tabs.getByRole('button', { name: '共识与分歧' }).click()
  await visible(finding(page, topic))
  await tabs.getByRole('button', { name: '发言实录' }).click()
  await visible(utteranceAt(page, topic, 3, false))
  await noHorizontalOverflow(page)
})
