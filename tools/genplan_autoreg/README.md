# Genplan Autoreg

Консервативный инструмент предварительной геопривязки растровых генпланов. Он:

1. получает WGS84 bbox населенного пункта из ЕГКН, затем из Nominatim;
2. загружает разрешенную подложку ArcGIS World Imagery или OSM с атрибуцией;
3. ищет локальные признаки SIFT/ORB;
4. оценивает homography через RANSAC;
5. сохраняет только **предлагаемые** GCP с confidence и диагностикой.

Инструмент **никогда не присваивает `approved`**. Любой результат имеет статус
`needs_manual` и должен быть проверен другим специалистом по независимым
контрольным точкам. Результат нельзя подключать к строгому поиску участков напрямую.

## Установка

Из корня проекта:

```powershell
.\.venv\Scripts\python.exe -m pip install -r tools\genplan_autoreg\requirements.txt
```

## Пилот Бурабая

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_autoreg `
  --source "C:\Users\medadmin\Documents\Codex\genplan\extracted\archive-002\Акмолинская область\Бурабайский район\Бурабай.jpg" `
  --output "C:\Users\medadmin\Documents\Codex\genplan\work\autoreg\burabay" `
  --locality "Бурабай" `
  --district "Бурабайский район" `
  --region "Акмолинская область" `
  --basemap arcgis `
  --zoom 15
```

Для полностью воспроизводимого запуска без геокодирования передайте bbox:

```powershell
.\.venv\Scripts\python.exe -m tools.genplan_autoreg `
  --source "path\to\plan.jpg" `
  --output "path\to\output" `
  --locality "Бурабай" `
  --bbox 70.20 52.97 70.38 53.13 `
  --basemap osm `
  --zoom 14
```

Порядок bbox: `WEST SOUTH EAST NORTH`.

## Результаты

- `result.json`: хеш исходника, bbox и его источник, лицензия подложки, метрики,
  причины `needs_manual` и proposed GCP;
- `plan_preview.jpg`: уменьшенная копия исходника;
- `basemap.jpg`: использованная подложка;
- `matches.jpg`: визуализация RANSAC-inlier совпадений, если homography найдена;
- `tile_cache/`: локальный кэш скачанных тайлов.

Confidence ограничен значением `0.79`, потому что автоматическая оценка не заменяет
независимый QA. GCP не создаются, если нельзя построить homography хотя бы по четырем
совпадениям. При слабом количестве, распределении или качестве совпадений результат
содержит причины отказа, не публикует GCP и остается `needs_manual`.

Если выбранный zoom требует более 144 тайлов, загрузчик автоматически понижает zoom
до безопасного размера и записывает фактически использованный уровень в
`basemap_source`, например `arcgis:z14`.

## Пороговые проверки

- минимум 24 candidate matches;
- минимум 12 RANSAC inliers;
- доля inliers не ниже 0.35;
- reprojection RMSE не выше 8 px;
- покрытие обеих карт совпадениями не ниже 8%;
- проверка обусловленности homography;
- не более 12 пространственно распределенных proposed GCP.

Даже прохождение всех порогов означает только пригодность предложения для ручной
проверки. Перед публикацией нужны независимые контрольные точки, анализ фактической
погрешности в метрах и подтверждение версии официального документа.
