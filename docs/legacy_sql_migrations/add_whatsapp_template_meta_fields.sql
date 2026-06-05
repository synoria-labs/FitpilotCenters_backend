-- Migration: Add Meta management fields to whatsapp_templates
-- Date: 2026-06-05
-- Description: The whatsapp_templates table (pre-existing, created by the external bot)
-- mirrors Meta Cloud API message templates. To support full create/edit/delete sync with
-- the WhatsApp Business Management API we need:
--   * category          -- required by Meta on create (AUTHENTICATION/MARKETING/UTILITY)
--   * meta_template_id   -- Meta's template id, used to edit/delete a specific template
-- Purely additive (ADD COLUMN IF NOT EXISTS) so it does not disturb the out-of-repo
-- realtime NOTIFY trigger on the messaging tables.

ALTER TABLE app.whatsapp_templates
ADD COLUMN IF NOT EXISTS category VARCHAR(30),
ADD COLUMN IF NOT EXISTS meta_template_id VARCHAR(64);

-- A template name is unique per language in Meta; mirror that locally to keep upserts sane.
CREATE UNIQUE INDEX IF NOT EXISTS uq_whatsapp_templates_name_language
ON app.whatsapp_templates(template_name, template_language);

COMMENT ON COLUMN app.whatsapp_templates.category IS 'Meta template category: AUTHENTICATION | MARKETING | UTILITY';
COMMENT ON COLUMN app.whatsapp_templates.meta_template_id IS 'Meta message-template id (for edit/delete via Business Management API)';
