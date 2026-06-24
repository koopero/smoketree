import { createApp, ref, onMounted } from 'vue';
import DriftPanel from './components/DriftPanel.js';
import RunBar from './components/RunBar.js';
import Browser from './components/Browser.js';

// Shell: a global RunBar (run button + streaming log) + drift banner, then the two-pane
// artifact Browser. The Browser owns all list/detail state; it delegates pipeline runs up
// to RunBar via `run`. After any run, `bust` bumps so the Browser + drift reload.
const App = {
  components: { DriftPanel, RunBar, Browser },
  setup() {
    const pipeline = ref('');
    const bust = ref(Date.now());
    const runBar = ref(null);
    const driftPanel = ref(null);

    function onRun(payload) {
      runBar.value?.run(payload);
    }

    async function afterRun() {
      bust.value = Date.now();
      await driftPanel.value?.refresh();
    }

    onMounted(async () => {
      try {
        const meta = await (await fetch('/api/meta')).json();
        pipeline.value = meta.pipeline || '';
      } catch { /* header just shows a blank pipeline name */ }
    });

    return { pipeline, bust, runBar, driftPanel, onRun, afterRun };
  },
  template: `
    <RunBar ref="runBar" :pipeline="pipeline" @done="afterRun" />
    <DriftPanel ref="driftPanel" @reconciled="afterRun" />
    <Browser :bust="bust" @run="onRun" />
  `,
};

createApp(App).mount('#app');
