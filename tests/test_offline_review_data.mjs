import assert from 'node:assert/strict';
import test from 'node:test';
import { generationInfo, generationSummary, queryOf } from '../scripts/offline_judge_mvp/review_data.mjs';

test('query aliases match Python priority and retain literal content', () => {
  assert.equal(queryOf({ query: '甲', Query: '乙', 问题: '丙' }), '甲');
  assert.equal(queryOf({ Query: '乙', 问题: '丙' }), '乙');
  assert.equal(queryOf({ 问题: '=literal + 0010' }), '=literal + 0010');
  assert.equal(queryOf({ query: '', Query: '备用内容' }), '');
  assert.equal(queryOf({ query: null }), 'None');
  assert.equal(queryOf({ query: false }), 'False');
  assert.throws(() => queryOf({ _sample_id: 'S001' }), /S001.*缺少 query/);
});

test('API source keeps recorded model configuration and metadata unchanged', () => {
  const draft = { _generation_meta: { origin: 'configured_api', model: 'judge-model',
    input_hash: 'original', config: { model: 'judge-model', base_url: 'https://example.invalid/v1' } } };
  const before = JSON.stringify(draft);
  const info = generationInfo(draft);
  assert.equal(info.category, 'api');
  assert.equal(info.model, 'judge-model');
  assert.equal(info.run, '外部API运行');
  const summary = generationSummary([draft]);
  assert.match(summary, /配置API生成1条/);
  assert.match(summary, /judge-model/);
  assert.match(summary, /https:\/\/example.invalid\/v1/);
  assert.doesNotMatch(summary, /ChatGPT/);
  assert.equal(JSON.stringify(draft), before);
});

test('current conversation is recognized only by recorded source', () => {
  const current = { _generation_meta: { provider: 'chatgpt_in_session',
    generation_method: 'current_model_draft', external_api_called: false } };
  assert.equal(generationInfo(current).category, 'chatgpt');
  assert.match(generationSummary([current]), /ChatGPT当前会话生成1条（非外部API运行）/);
  assert.equal(generationInfo({}).category, 'unknown');
  assert.equal(generationInfo({ _generation_meta: { provider: 'other' } }).category, 'unknown');
  assert.equal(generationInfo({ _generation_meta: { ...current._generation_meta,
    external_api_called: true } }).category, 'unknown');
});

test('mixed and unknown batches describe all source groups', () => {
  const drafts = [
    { _generation_meta: { provider: 'chatgpt_in_session', external_api_called: false } },
    { _generation_meta: { origin: 'configured_api', model: 'model-a', config: { base_url: 'https://a.invalid/v1' } } },
    { _generation_meta: { origin: 'configured_api', model: 'model-b', config: { base_url: 'https://b.invalid/v1' } } },
    {},
  ];
  const summary = generationSummary(drafts);
  assert.match(summary, /来源混合/);
  assert.match(summary, /ChatGPT当前会话生成1条/);
  assert.match(summary, /配置API生成2条，2种配置/);
  assert.match(summary, /model-a/);
  assert.match(summary, /model-b/);
  assert.match(summary, /来源或运行方式待确认1条/);
  assert.match(generationSummary([{}]), /来源或运行方式待确认1条/);
  assert.doesNotMatch(generationSummary([{}]), /ChatGPT|外部API运行/);
});

test('missing API details stay explicitly unknown, rather than inventing configuration', () => {
  const info = generationInfo({ _generation_meta: { origin: 'configured_api' } });
  assert.equal(info.model, '未记录的API模型');
  assert.match(info.configuration, /未记录endpoint/);
});
