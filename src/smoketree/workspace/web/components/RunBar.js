// The Run button plus the streaming log. Owns the run lifecycle: POST /api/run,
// read the text/plain stream chunk by chunk into a reactive log, then tell the
// parent to refresh (bump cache-bust, re-fetch index + drift). Exposes run() so a
// re-roll elsewhere can trigger the same flow.
//
// The Rules menu lets a human run ONLY selected rules (POST /api/run {only:[…]}),
// rather than the whole pipeline — showing each rule's pending-work count from
// /api/graph so you can pick (or one-click "stale") just the stages you want.
import { ref, computed, onMounted, nextTick } from 'vue';

export default {
  emits: ['done'],
  setup(props, { emit, expose }) {
    const running = ref(false);
    const log = ref('');
    const showLog = ref(false);
    const logEl = ref(null);
    const rules = ref([]);
    const selected = ref([]);
    const menuOpen = ref(false);

    const runCount = (r) => (r.instances || []).filter((i) => i.state === 'RUN').length;
    const staleRules = computed(() => rules.value.filter((r) => runCount(r) > 0));

    async function fetchRules() {
      try {
        const g = await (await fetch('/api/graph')).json();
        rules.value = g.rules || [];
        // drop selections for rules that no longer exist
        const names = new Set(rules.value.map((r) => r.name));
        selected.value = selected.value.filter((n) => names.has(n));
      } catch (e) { /* leave rules as-is */ }
    }

    function selectStale() { selected.value = staleRules.value.map((r) => r.name); }

    async function append(chunk) {
      log.value += chunk;
      await nextTick();
      if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight;
    }

    // payload: optional { only?: [rule], where?: {key: val} } to run a subset/cell;
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
        emit('done');          // parent bumps bust + re-fetches index/drift
        fetchRules();          // refresh pending-work counts after the run
      }
    }

    function runSelected() {
      if (!selected.value.length || running.value) return;
      menuOpen.value = false;
      run({ only: [...selected.value] });
    }

    expose({ run });
    onMounted(fetchRules);
    return { running, log, showLog, logEl, run, rules, selected, menuOpen,
             runCount, staleRules, selectStale, runSelected };
  },
  template: `
    <header>
      <div>
        <h1>smoketree workspace</h1>
        <div class="sub">pipeline <strong>{{ pipeline }}</strong> · note an output, then Run to apply</div>
      </div>
      <div class="run-actions">
        <div class="rule-picker">
          <button type="button" class="ghost" @click="menuOpen = !menuOpen">
            ▤ Rules<span v-if="selected.length"> ({{ selected.length }})</span>
          </button>
          <div class="rule-menu" v-show="menuOpen">
            <div class="rule-menu-head">
              <span>{{ staleRules.length }} with pending work</span>
              <span>
                <a href="#" @click.prevent="selectStale">stale</a> ·
                <a href="#" @click.prevent="selected = []">none</a>
              </span>
            </div>
            <label v-for="r in rules" :key="r.name" class="rule-row" :class="{ work: runCount(r) > 0 }">
              <input type="checkbox" :value="r.name" v-model="selected">
              <span class="rule-name">{{ r.name }}</span>
              <span class="rule-badge" v-if="runCount(r) > 0">{{ runCount(r) }} to run</span>
              <span class="rule-badge dim" v-else>{{ r.state }}</span>
            </label>
            <button type="button" class="run-selected" :disabled="!selected.length || running"
                    @click="runSelected">▶ Run {{ selected.length }} selected</button>
          </div>
        </div>
        <button id="run" type="button" :disabled="running" @click="run()">{{ running ? '… running' : '▶ Run all' }}</button>
      </div>
    </header>
    <pre id="log" ref="logEl" v-show="showLog">{{ log }}</pre>
  `,
  props: {
    pipeline: { type: String, default: '' },
  },
};
