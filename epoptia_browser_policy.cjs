'use strict';

// Source-controlled origin; reader session activation remains separately controlled.
module.exports = Object.freeze({
  origin: require('./epoptia_login_policy.cjs').origin,
  sessionFile: null,
  pages: Object.freeze({
    workorders: '/workorders',
    workorderlines: '/workorderlines',
    production_report: '/reports/factory/productiondata',
    daily_analysis: '/reports/factory/dailyanalysis',
  }),
  // Extend only after reviewing exact GET endpoints/assets for side effects.
  assets: Object.freeze([]),
});
