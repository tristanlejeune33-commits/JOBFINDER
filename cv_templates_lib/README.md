# Bibliothèque de templates CV

Ce dossier contient tous les templates HTML utilisables dans CV Studio.
**Auto-découverte** : tout fichier `.html` ajouté ici apparaît automatiquement dans la bibliothèque, sans modifier le code Python.

## Comment ajouter un template

1. Crée un fichier `.html` dans ce dossier (ex: `mon_template.html`)
2. **Mets une balise meta en tête** (1ère ligne juste après `<!DOCTYPE html>`) :

   ```html
   <!-- meta: {"name":"Mon Template","category":"Créatif","preview":"Style très coloré"} -->
   ```

   Champs supportés :
   - `name` : nom affiché dans la bibliothèque
   - `category` : catégorie pour le filtre (ex: `Moderne`, `Premium`, `Créatif`, `Tech`, `Académique`, `Minimaliste`)
   - `preview` : description courte (1 phrase)
   - `tags` : tableau de tags optionnels (ex: `["dark", "monospace"]`)

3. Utilise les **placeholders Mustache-like** dans ton HTML :

## Variables disponibles

### Strings simples (interpolation directe)
```
{{name}}              -> Nom complet
{{title}}             -> Poste actuel / titre
{{summary}}           -> Accroche
{{photo_or_initials}} -> <img src="..."> si photo, sinon initiales
{{color}}             -> Couleur primaire choisie (hex)
{{color_dark}}        -> Variante foncée auto-générée
{{color_light}}       -> Variante claire auto-générée
{{contact.email}}     -> Email
{{contact.phone}}     -> Téléphone
{{contact.location}}  -> Ville
{{contact.linkedin}}  -> LinkedIn
{{contact.website}}   -> Site web
```

### Sections (truthy/list)
```
{{#name}}...{{/name}}                -> Rendu si name non-vide
{{^name}}...{{/name}}                -> Rendu si name VIDE (inversé)
{{#has_experience}}...{{/has_experience}}    -> True si liste non vide
{{#has_education}}...{{/has_education}}
{{#has_skills}}...{{/has_skills}}
{{#has_languages}}...{{/has_languages}}
{{#has_certifications}}...{{/has_certifications}}
{{#has_interests}}...{{/has_interests}}
```

### Boucles
```html
{{#experience}}
  <h3>{{role}}</h3>
  <p>{{company}} · {{location}} · {{date}}</p>
  {{#has_bullets}}
    <ul>{{#bullets}}<li>{{.}}</li>{{/bullets}}</ul>
  {{/has_bullets}}
{{/experience}}

{{#education}}
  <p>{{degree}} — {{school}} ({{date}})</p>
{{/education}}

{{#skills}}
  <span>{{name}} ({{level}}/5, {{level_pct}}%)</span>
{{/skills}}

{{#languages}}
  <p>{{name}} : {{level}}</p>
{{/languages}}

{{#certifications}}
  <p>{{name}} ({{date}})</p>
{{/certifications}}

{{#interests}}
  <span>{{.}}</span>
{{/interests}}
```

## Conseils pour des templates qui rendent bien

- Format A4 : `body max-width:794px; min-height:1123px;`
- Print : ajoute `@media print { body{background:#fff} @page{size:A4;margin:0} }`
- Utilise `var(--primary)`, `var(--primary-d)`, `var(--primary-l)` (déjà injectés par le moteur)
- Pas de JS — l'iframe est sandboxé `allow-same-origin` (no-script)
- Pas d'images externes (sauf Google Fonts)
- Un fichier HTML = un template autonome (CSS dans `<style>`, pas de fichiers liés)

## Templates "core" fournis

| ID | Nom | Catégorie |
|---|---|---|
| modern | Moderne | Moderne |
| editorial | Éditorial | Premium |
| bold | Bold | Créatif |
| tech | Tech | Tech |
| premium | Premium | Premium |
| creative | Créatif | Créatif |

Les noms cachés commençant par `_` (ex: `_partial.html`) ne sont pas listés dans la bibliothèque.
