# توصيات تطوير المعمارية — AI Forklift Safety (v3 → v4)

> مراجعة هندسية لتحويل السيستم من نموذج تجريبي إلى سيستم صناعي يعمل فيزيائيًا على Toyota 8FB25.
> تمت مراجعة هذه التوصيات بواسطة مراجعة مستقلة بعدسة functional safety (ISO 13849 / IEC 62061).

## الخلاصة

لغة البرمجة ليست المشكلة الأساسية. المشكلة أن وظيفة الأمان كلها تعيش داخل PC + Linux + Python + USB + ريلاي استهلاكي، وأي عنصر منها يقع = الرافعة تكمل شغالة. الحل: فصل السيستم إلى 3 طبقات، وأهم طبقة فيها بدون software.

## 1. نقاط الفشل الفيزيائية

| السيناريو | ما يحدث الآن | الحل |
|---|---|---|
| انقطاع الكهرباء / الـ PC يطفي | الريلاي يفصل = "لا يوجد شخص" → الرافعة تمشي (fail-dangerous) | De-energize-to-trip: السرعة الكاملة متاحة فقط طالما توجد إشارة OK |
| Linux يعلق / hardware_worker يموت | الـ watchdog البرمجي في v3 يموت معه | Pulse watchdog relay خارجي (hardware) |
| هبوط البطارية، spikes من الـ regen، arcing في الـ contactor | reset للـ PC أو فصل USB hub | Isolated wide-input DC-DC + TVS + supercap hold-up |
| بطارية 48V غالبًا floating عن الشاسيه | ربط ground الـ PC أو الـ shields بالشاسيه = leakage / insulation fault | مصدر معزول + خطة grounding مقصودة |
| EMI من الـ inverter والموتور | USB re-enumeration، الريلاي يختفي من /dev/ttyUSB0 | إخراج USB من مسار الأمان، كابلات shielded، اختبار EMC حسب EN 12895 |
| الاهتزاز | فك كابلات USB، RM04U غير مصمم للاهتزاز | M12 / FAKRA locking، DIN-rail I/O، shock mounts، eMMC/NVMe بدل SD |
| الحرارة / البرودة / cold rooms | تكثيف على العدسة، مراوح تسحب الأتربة | Fanless IP65، heated camera windows، conformal coating |
| تنظيف VHP / IPA | تلف housings الكاميرات | housings مقاومة كيميائيًا |
| الكاميرا تتجمد على نفس الفريم | الـ AI يرى منطقة فارغة دائمًا = "آمن" | frozen-frame detection + فحص timestamp/sequence |
| عدسة مغطاة / متسخة / الكاميرا انحرفت | نفس المشكلة | فحص blockage / defocus / displacement مقابل صورة مرجعية → المنطقة تذهب لـ safe state |
| عودة الكهرباء | الـ boot يأخذ 30–60 ثانية والرافعة مكشوفة | BIOS "power on after AC loss" + الرافعة على creep speed حتى يصبح السيستم ready |
| انقطاع أثناء كتابة ملف | تلف config.json / security.json | Read-only rootfs (overlayroot) + partition منفصل للبيانات |
| الـ OS لا يقلع | السيستم ميت والرافعة شغالة | de-energize يجعل هذه الحالة آمنة تلقائيًا + A/B partitions |
| gowns بيضاء أمام حوائط بيضاء، انعكاسات epoxy | YOLOv8n (COCO) يفوّت الأشخاص | Fine-tune على فيديوهات من الموقع + قياس recall فعلي |
| حمل مرفوع | يحجب رؤية الكاميرا الأمامية للأرض | زاوية كاميرا إضافية + radar |
| فصل الـ seat circuit | على أغلب الـ AC controllers الرافعة تكمل بالقصور الذاتي ولا تفرمل، وقد تطلع fault يتم التحايل عليه | تأكيد إلزامي من Toyota قبل الاعتماد عليه |
| False trips كثيرة | السائقون يتحايلون / يفصلون السيستم | creep بدل stop عند الـ fault، bypass بمفتاح + مؤقت + تسجيل، KPI لمعدل الـ false trips |

## 2. تصحيحات على v3

1. الـ watchdog في v3 يحمي من crash الواجهة فقط. لو الـ PC وقع أو الكهرباء قطعت أو board الريلاي انفصل من USB، الـ watchdog يموت معهم. لا يوجد software watchdog يحل ذلك؛ الحل hardware.
2. الـ heartbeat المستقل عن الـ detection قرار غير صحيح لهذا الغرض. يجب أن يتولد النبض بعد معالجة فريم جديد من كل كاميرا؛ لو أي stream توقف → النبض يتوقف.
3. الـ AI vision لن يصل لمستوى أمان معتمد (تقدير المراجعة المستقلة: PL b كحد أقصى ومع channel ثانٍ مستقل). يجب توثيق السيستم كـ **collision-warning aid تكميلي**، والضوابط الأساسية تبقى: فصل ممرات المشاة، تحديد السرعة، blue-spot lights.

## 3. المعمارية المقترحة (3 طبقات)

### طبقة 1 — الأمان (hardware فقط)
- De-energize-to-trip.
- Pulse watchdog relay / safety relay بـ dynamic input: توقف الـ square wave أو ثباته → trip.
- Force-guided relays + EDM.
- أي fault → creep speed وليس hard stop.
- الربط مع الرافعة عبر speed-reduction input معتمد من Toyota dealer بدل قطع الـ seat circuit.

### طبقة 2 — الرؤية
- Edge AI box صناعي: NVIDIA Jetson Orin، fanless، wide-voltage DC، -20 إلى +60°C.
- TensorRT / DeepStream: تقديريًا 15–30 FPS لكل كاميرا وأقل من 100ms (مقابل 2–3 FPS و300ms حاليًا) — تُقاس فعليًا.
- كاميرات GMSL2 أو PoE بـ M12، بدون USB. Isolated digital I/O بدل RM04U.
- Radar على safety logic منفصلة (ليس نفس الـ PC أو الباور). ملاحظة: mmWave لا يفرّق بين شخص والـ racking.
- Camera health monitor لكل stream.
- Tracking + hysteresis (trip سريع، release بطيء) + zones على مستوى الأرض + طول zone يتغير مع السرعة.

### طبقة 3 — الواجهة والبيانات
- Web HMI محلي؛ سقوط الواجهة لا يؤثر على الـ detection.
- Event clips + near-miss log + heatmap → KPI لـ HSE.
- OS read-only + A/B updates + systemd WatchdogSec + SoC hardware watchdog.

### بخصوص Python

| الجزء | التقنية |
|---|---|
| قرار الأمان | Hardware (watchdog / safety relay) — بدون كود |
| الكاميرات والـ AI | C++ عبر DeepStream / TensorRT |
| Health checks و logging | Python |
| الواجهة | Web محلي |
| الـ OS | Linux read-only، وليس Windows |

الهدف ليس تغيير Python، بل إخراج Python والـ PC من مسار الأمان.

## 4. خطة التنفيذ

| المرحلة | المحتوى | التكلفة |
|---|---|---|
| 0 — فورًا | تحديد أقصى سرعة من Toyota dealer في مناطق المشاة (مسافة الوقوف تتناسب مع مربع السرعة). سؤال Toyota عن سلوك فصل الـ seat circuit على 8FB25 | شبه صفر |
| 1 — أسابيع | De-energize-to-trip، pulse watchdog relay، heartbeat مرتبط بمعالجة الفريمات، frozen/blocked checks، auto power-on، read-only FS، isolated DC-DC + supercap | منخفضة |
| 2 — 1–3 شهور | Jetson + GMSL2/PoE + TensorRT، fine-tune على بيانات الموقع، radar على channel مستقل، web HMI | متوسطة |
| 3 — بالتوازي | URS، FMEA، IQ/OQ/PQ مع fault injection، proof test يومي، change control، privacy notice و retention policy (قانون حماية البيانات 151/2020) | جهد هندسي + HSE |

مثال حسابي: عند ~4.5 م/ث، 300ms latency + فريمين تأكيد ≈ 2 متر قبل بدء الفرملة. طول الـ zone يُحسب من مسافة وقوف مُقاسة.

## 5. قرارات مطلوبة
1. الهدف: aid تكميلي أم safety function رسمي (يتطلب تقييم ISO 13849 من جهة مؤهلة)؟
2. ميزانية المرحلة 2 أم الاكتفاء بالمرحلة 1 على الـ hardware الحالي؟
3. التواصل مع Toyota dealer بخصوص حد السرعة والـ interface المعتمد.
