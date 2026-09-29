import assert from 'node:assert/strict'
import { test } from 'node:test'
import { build } from 'esbuild'
import { readFileSync } from 'node:fs'

const { outputFiles } = await build({
  entryPoints: [new URL('./src/lib/utils.ts', import.meta.url).pathname],
  bundle: true, write: false, platform: 'node', format: 'esm',
})
const { audioDownloadName } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`)

test('中文、安全清理、后缀与旧任务回退', () => {
  for (const kind of ['mono', 'podcast']) {
    const url = `/api/${kind}/audio/task-id`
    assert.equal(audioDownloadName('中文项目', url), '中文项目.wav')
    assert.equal(audioDownloadName(undefined, url), `${kind}_task-id.wav`)
    assert.equal(audioDownloadName('中文/\\:*?"<>|\r\n\0\x7f', url), `中文${'_'.repeat(13)}.wav`)
    assert.equal(audioDownloadName(' .. ', url), 'audio.wav')
    assert.equal(audioDownloadName('CON', url), '_CON.wav')
    assert.equal(audioDownloadName('节目.WAV.wav ', url), '节目.wav')
    assert.equal(Buffer.byteLength(audioDownloadName('中'.repeat(200), url)), 244)
  }
})

test('单人与双人页面使用同一队列下载入口', () => {
  for (const page of ['DubbingPage', 'PodcastPage']) {
    const source = readFileSync(new URL(`./src/pages/${page}.tsx`, import.meta.url), 'utf8')
    assert.match(source, /<QueuePanel/)
    assert.match(source, /project_name: name/)
  }
  const queue = readFileSync(new URL('./src/components/QueuePanel.tsx', import.meta.url), 'utf8')
  assert.match(queue, /href=\{task.audio_url\}/)
  assert.match(queue, /download=\{audioDownloadName\(task.project_name, task.audio_url\)\}/)
})
