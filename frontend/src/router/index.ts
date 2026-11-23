import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import StrataDispatchView from '@/views/StrataDispatchView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/strata_dispatch', name: 'strata_dispatch', component: StrataDispatchView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
