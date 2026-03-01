// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
	site: 'https://sairo.dev',
	integrations: [
		starlight({
			title: 'Sairo',
			description: 'Self-hosted S3-compatible object storage browser',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/AshwathStephen/sairo' },
			],
			customCss: ['./src/styles/custom.css'],
			head: [
				{
					tag: 'meta',
					attrs: { property: 'og:image', content: 'https://sairo.dev/og-image.png' },
				},
			],
			sidebar: [
				{
					label: 'Getting Started',
					items: [
						{ slug: 'getting-started/quickstart' },
						{ slug: 'getting-started/concepts' },
					],
				},
				{
					label: 'Installation',
					items: [
						{ slug: 'installation/docker' },
						{ slug: 'installation/helm' },
						{ slug: 'installation/configuration' },
					],
				},
				{
					label: 'Features',
					collapsed: false,
					items: [
						{ slug: 'features/object-browser' },
						{ slug: 'features/search' },
						{ slug: 'features/file-preview' },
						{ slug: 'features/upload-download' },
						{ slug: 'features/storage-dashboard' },
						{ slug: 'features/versioning' },
						{ slug: 'features/bucket-settings' },
						{ slug: 'features/share-links' },
						{ slug: 'features/multi-endpoint' },
						{ slug: 'features/keyboard-shortcuts' },
					],
				},
				{
					label: 'Security',
					items: [
						{ slug: 'security/authentication' },
						{ slug: 'security/user-management' },
						{ slug: 'security/two-factor' },
						{ slug: 'security/api-tokens' },
						{ slug: 'security/oauth-ldap' },
						{ slug: 'security/audit-log' },
					],
				},
				{
					label: 'CLI',
					items: [
						{ slug: 'cli/quickstart' },
						{ slug: 'cli/commands' },
					],
				},
				{
					label: 'Provider Guides',
					collapsed: true,
					items: [
						{ slug: 'guides/aws-s3' },
						{ slug: 'guides/minio' },
						{ slug: 'guides/cloudflare-r2' },
						{ slug: 'guides/reverse-proxy' },
					],
				},
				{
					label: 'Reference',
					items: [
						{ slug: 'reference/api' },
						{ slug: 'reference/environment-variables' },
						{ slug: 'reference/architecture' },
						{ slug: 'reference/benchmarks' },
						{ slug: 'reference/changelog' },
					],
				},
			],
		}),
	],
});
