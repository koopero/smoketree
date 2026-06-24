// The Run button plus the streaming log. Owns the run lifecycle: POST /api/run,
// read the text/plain stream chunk by chunk into a reactive log, then tell the
// parent to refresh (bump cache-bust, re-fetch index + drift). Exposes run() so a
// re-roll elsewhere can trigger the same flow.
import { ref, nextTick } from 'vue';

export default {
  emits: ['done'],
  setup(props, { emit, expose }) {
    const running = ref(false);
    const log = ref('');
    const showLog = ref(false);
    const logEl = ref(null);

    async function append(chunk) {
      log.value += chunk;
      await nextTick();
      if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
    }

    // payload: optional { only?: [rule], where?: {key: val} } to run a single rule/cell;
    // omitted ⇒ full pipeline run.
    async function run(payload) {
      if (running.value) return;
      running.value = true;
      showLog.value = true;
      log.value = '';
      try {
        const opts = { method: 'POST' };
        if (payload && (payload.only || payload.where)) {
          opts.headers = { 'Content-Type': 'application/json' };
          opts.body = JSON.stringify(payload);
        }
        const resp = await fetch('/api/run', opts);
        if (resp.status === 409) {
          log.value = 'A run is already in progress.';
          return;
        }
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          await append(dec.decode(value, { stream: true }));
        }
      } catch (e) {
        await append('\n[error] ' + e);
      } finally {
        running.value = false;
        emit('done');  // parent bumps bust + re-fetches index/drift
      }
    }

    expose({ run });
    return { running, log, showLog, logEl, run };
  },
  template: `
    <header>
      <div>
        <h1>smoketree workspace</h1>
        <div class="sub">pipeline <strong>{{ pipeline }}</strong> · note an output, then Run to apply</div>
      </div>
      <button id="run" type="button" :disabled="running" @click="run()">{{ running ? '… running' : '▶ Run pipeline' }}</button>
    </header>
    <pre id="log" ref="logEl" v-show="showLog">{{ log }}</pre>
  `,
  props: {
    pipeline: { type: String, default: '' },
  },
};
