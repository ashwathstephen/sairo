/**
 * Centralized DOM selectors for Sairo E2E tests.
 * Derived from actual component source (Login.jsx, App.jsx, BucketList.jsx, etc.)
 */
export const SEL = {
  // ── Login ──
  usernameInput: 'input[aria-label="Username"]',
  passwordInput: 'input[aria-label="Password"]',
  signInButton: 'button.btn-primary',
  loginError: '.login-error',
  loginForm: '.login-form',
  loginPage: '.login-page',
  tfaCodeInput: '.tfa-login-input',
  recoveryCodeToggle: 'text="Use a recovery code"',
  authToggleLdap: '.auth-toggle-btn:has-text("LDAP")',
  authToggleLocal: '.auth-toggle-btn:has-text("Local")',
  oauthDivider: '.login-oauth-divider',
  oauthButtons: '.login-oauth-buttons',

  // ── Header ──
  headerTitle: 'header h1',
  headerLeft: '.header-left',
  headerRight: '.header-right',
  userBadge: '.user-badge',
  userName: '.user-name',
  userRole: '.user-role',
  logoutButton: '.user-badge button:has-text("Logout")',
  tfaHeaderButton: '.user-badge button:has-text("2FA")',
  bucketNameSpan: '.bucket-name',

  // ── Admin buttons (bucket list view) ──
  usersButton: 'button.btn-settings:has-text("Users")',
  apiTokensButton: 'button.btn-settings:has-text("API Tokens")',
  licenseButton: 'button.btn-settings:has-text("License")',
  activityButton: 'button.btn-settings:has-text("Activity")',
  healthButton: 'button.btn-settings:has-text("Health")',
  endpointsButton: 'button.btn-settings:has-text("Endpoints")',

  // ── Bucket List ──
  bucketCard: '.bucket-card',
  bucketCardName: '.bucket-card-name',
  bucketCardMeta: '.bucket-card-meta',
  bucketDeleteBtn: '.bucket-delete-btn',
  bucketGrid: '.bucket-grid',
  createBucketBtn: 'button:has-text("+ Create Bucket")',
  createBucketInput: 'input[placeholder="new-bucket-name"]',
  createBucketSubmit: '.create-bucket-bar button.btn-primary',
  createBucketCancel: '.create-bucket-bar button:has-text("Cancel")',
  bucketCount: '.count',

  // ── Object Browser ──
  breadcrumb: '.breadcrumb',
  tableRow: '.table-row',
  folderRow: '.table-row.row-folder',
  folderLink: '.folder-link',
  colName: '.col-name',
  colSize: '.col-size',
  colModified: '.col-modified',
  colActions: '.col-actions',
  filterInput: 'input[placeholder="Filter by name..."]',
  sortableName: '.th.sortable >> nth=0',
  sortableSize: '.th.sortable >> nth=1',
  sortableModified: '.th.sortable >> nth=2',
  selectAllCheckbox: '.table-header-row .col-check input[type="checkbox"]',
  tableHeaderRow: '.table-header-row',
  emptyState: '.empty-state',
  tableFooter: '.table-footer',

  // ── Toolbar ──
  toolbar: '.toolbar',
  toolbarActions: '.toolbar-actions',
  refreshButton: 'button[aria-label="Refresh"]',
  showDeletedButton: 'button:has-text("Show Deleted")',
  hideDeletedButton: 'button:has-text("Hide Deleted")',
  uploadButton: 'button:has-text("Upload")',
  newFolderButton: 'button:has-text("New Folder")',
  deleteToolbarButton: '.toolbar-actions button.btn-danger',
  searchButton: 'button[aria-label="Search"]',
  dashboardButton: 'button[aria-label="Storage Dashboard"]',
  settingsButton: 'button[aria-label="Bucket Settings"]',
  helpButton: 'button[aria-label="Keyboard shortcuts"]',

  // ── Progress ──
  progressBar: '.progress-bar',
  cacheBadge: '.cache-badge',

  // ── Upload Modal ──
  dropZone: '.drop-zone',
  uploadFileInput: '.modal input[type="file"]',
  uploadSubmitButton: '.modal-actions button.btn-primary',
  uploadCancelButton: '.modal-actions button:has-text("Cancel")',
  uploadProgressFill: '.upload-progress-fill',
  uploadFileRow: '.upload-file-row',
  uploadFileCancelBtn: '.upload-file-cancel',
  uploadSummary: '.upload-summary',

  // ── Modals ──
  modalOverlay: '.modal-overlay',
  modal: '.modal',
  modalActions: '.modal-actions',
  modalCloseButton: '.modal-actions button:has-text("Close")',
  modalDismissButton: '.modal button:has-text("×")',

  // ── Delete Dialog ──
  deleteConfirmButton: '.modal button.btn-danger:has-text("Delete")',
  deleteCancelButton: '.modal .modal-actions button:not(.btn-danger)',

  // ── Prompt Dialog ──
  promptInput: '.modal input[type="text"]',
  promptSubmit: '.modal button.btn-primary',
  promptCancel: '.modal button:has-text("Cancel")',

  // ── Search ──
  searchInput: '.search-input',
  searchItem: '.search-item',
  searchCount: '.search-count',
  searchEmpty: '.search-empty',
  searchError: '.search-error',
  searchHint: '.search-hint',
  searchList: '.search-list',
  searchItemName: '.search-item-name',

  // ── Tabs ──
  tabBar: '.tab-bar',
  tabButton: '.tab-btn',
  tabActive: '.tab-active',

  // ── ObjectInfo ──
  infoTable: '.info-table',
  infoLabel: '.info-label',
  infoValue: '.info-value',

  // ── Bulk bar ──
  bulkBar: '.bulk-bar',
  bulkBarCount: '.bulk-bar-count',
  bulkCopyBtn: '.bulk-bar button:has-text("Copy to...")',
  bulkMoveBtn: '.bulk-bar button:has-text("Move to...")',
  bulkDeleteBtn: '.bulk-bar button.btn-danger',

  // ── Folder Picker ──
  folderPickerBucketSelect: '.folder-picker select',
  folderPickerConfirm: '.modal button.btn-primary',

  // ── Toast ──
  toast: '.toast',
  toastSuccess: '.toast-success',
  toastError: '.toast-error',
  toastWarning: '.toast-warning',

  // ── User Management ──
  addUserButton: '.modal button.btn-primary:has-text("Add User")',
  formError: '.form-error',

  // ── Favorites ──
  favoriteStar: '.breadcrumb .btn-favorite',

  // ── Theme ──
  themeToggle: '.theme-toggle',

  // ── Drag & Drop ──
  dropOverlay: '.drop-overlay',
  dropOverlayText: '.drop-overlay-text',

  // ── Welcome ──
  welcomeGotIt: 'button:has-text("Got it")',

  // ── Deleted section ──
  deletedSection: '.deleted-section',
  deletedRow: '.table-row.row-deleted',
  purgeButton: 'button.btn-danger:has-text("Purge")',

  // ── Keyboard shortcuts modal ──
  shortcutRow: '.shortcut-row',

  // ── Version badges ──
  versionBadgeLatest: '.version-badge-latest',
  versionBadgeOld: '.version-badge-old',
  versionBadgeDeleted: '.version-badge-deleted',

  // ── Settings tabs ──
  statusBadge: '.status-badge',
  statusOn: '.status-on',
  statusOff: '.status-off',
  reindexButton: 'button:has-text("Re-index")',
  codeBlock: 'textarea.code-block',

  // ── Health Check ──
  hcSysBanner: '.hc-sys-banner',
  hcSysOk: '.hc-sys-ok',
  hcGrid: '.hc-grid',
  hcCard: '.hc-card',

  // ── Dashboard / Insights ──
  dashboardModal: '.dashboard-modal',
  dashboardCard: '.dashboard-card',
  dashboardCardValue: '.dashboard-card-value',
  dashboardCardLabel: '.dashboard-card-label',
  dashboardBarRow: '.dashboard-bar-row',
  dashboardBarLabel: '.dashboard-bar-label',
  dashboardTable: '.dashboard-table',
  trendToggleBtn: '.trend-toggle-btn',
  trendChartSvg: '.trend-chart-svg',
  insightsStorageTab: '.dashboard-modal button:has-text("Storage")',
  insightsOptimizeTab: '.dashboard-modal button:has-text("Optimize")',

  // ── Optimization ──
  optimizationSpinner: '.dashboard-modal .spinner',
  optimizationEmpty: '.dashboard-modal .muted',
  optimizationSection: '.dashboard-modal h4',
  severityBadge: '.severity-badge',
  coldDataTable: '.cold-data-table',
  accuracyDisclaimer: '.accuracy-disclaimer',

  // ── Update Banner ──
  updateBanner: '.update-banner',
  updateDismiss: '.update-banner button',

  // ── Version actions ──
  versionBusy: '.version-busy',

  // ── BucketSettings mutations ──
  savePolicyBtn: '.modal button.btn-primary:has-text("Save Policy")',
  deletePolicyBtn: '.modal button.btn-danger:has-text("Delete Policy")',
  saveCorsBtn: '.modal button.btn-primary:has-text("Save CORS")',
  deleteCorsBtn: '.modal button.btn-danger:has-text("Delete CORS")',
  saveLifecycleBtn: '.modal button.btn-primary:has-text("Save Changes")',
  lcCard: '.lc-card',

  // ── User Management ──
  roleSelect: '.role-select',

  // ── CrawlStatus ──
  crawlBadge: '.crawl-badge',
  crawlDropdown: '.crawl-dropdown',
  crawlDetail: '.crawl-detail',
  crawlReindexBtn: '.crawl-dropdown button.btn-primary',

  // ── FolderPicker ──
  folderPickerPathInput: '.folder-picker-path input',
  folderPickerGoBtn: '.folder-picker-path button',
  folderPickerItem: '.folder-picker-item',

  // ── Share link form ──
  sharePasswordInput: '.modal input[placeholder="Password (optional)"]',
  shareMaxDlInput: '.modal input[type="number"][placeholder="Max downloads"]',
  shareCreateBtn: '.modal button:has-text("Create Link")',

  // ── Presigned URL ──
  urlInput: '.url-input',
  presignedCopyBtn: '.presigned-url button.btn-primary',

  // ── Toast actions ──
  toastAction: '.toast-action',
  toastClose: '.toast-close',

  // ── FilePreview ──
  previewIframe: '.preview-iframe',
  schemaPreview: '.schema-preview',
  schemaTable: '.schema-table',
  schemaBadge: '.schema-badge',

  // ── Sort columns ──
  sortableColSize: '.th.col-size.sortable',
  sortableColModified: '.th.col-modified.sortable',

  // ── UI Improvements (v2) ──
  fileIcon: 'svg.file-icon',
  folderIcon: 'svg.folder-icon',
  densityToggle: '.density-toggle',
  searchItemActive: '.search-item-active',
  searchHighlight: 'mark.search-highlight',
  searchKbd: '.search-header .kbd',
  streamingDot: '.streaming-dot',
  streamingIndicator: '.streaming-indicator',
  streamingCount: '.streaming-count',
  progressActive: '.progress-bar.progress-active',
  progressDone: '.progress-bar.progress-done',
  uploadFileStatus: '.upload-file-status',
} as const;
