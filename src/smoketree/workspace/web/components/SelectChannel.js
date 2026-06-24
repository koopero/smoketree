import { postJSON } from '../util.js';

// Pill options; clicking one POSTs the new value and updates the active state.
export default {
  props: {
    card: { type: Object, required: true },
    channel: { type: Object, required: true },
  },
  emits: ['flagchange'],
  setup(props, { emit }) {
    async function choose(value) {
      try {
        await postJSON('/api/select', {
          id: props.card.id,
          channel: props.channel.name,
          value,
        });
      } catch {
        return;
      }
      props.channel.value = value;
      emit('flagchange');
    }
    return { choose };
  },
  template: `
    <div class="channel">
      <div class="chead">{{ channel.name }}<template v-if="channel.describe"> — <span class="desc">{{ channel.describe }}</span></template></div>
      <div class="select">
        <button v-for="o in channel.options" :key="o" type="button"
                class="opt" :class="{ active: o === channel.value }"
                @click="choose(o)">{{ o }}</button>
      </div>
    </div>
  `,
};
