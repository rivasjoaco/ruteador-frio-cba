import streamlit as st
import pandas as pd
import numpy as np
import urllib.parse
import re
import googlemaps
from sklearn.cluster import KMeans
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

st.set_page_config(
    page_title="Torre de Control - Servicio Técnico Frío",
    page_icon="🚚",
    layout="wide"
)

# Configuración de Centros y Depósitos Base
CENTROS_CONFIG = {
    "Córdoba (Centro 0960)": {
        "codigos": ["0960", "960"],
        "depot_address": "Depósito San Isidro EDASA Coca Cola X5016 Córdoba, Argentina",
        "depot_coords": (-31.442, -64.148),
        "provincia": "Córdoba",
        "ciudad": "Córdoba Capital"
    },
    "Mendoza (Centro 0962)": {
        "codigos": ["0962", "962"],
        "depot_address": "Alsina 2336, M5501 Godoy Cruz, Mendoza, Argentina",
        "depot_coords": (-32.923, -68.835),
        "provincia": "Mendoza",
        "ciudad": "Mendoza Capital"
    },
    "Litoral (Centro 0961)": {
        "codigos": ["0961", "961"],
        "depot_address": "Depósito Base Litoral, Rosario, Santa Fe, Argentina",
        "depot_coords": (-32.9468, -60.6393), 
        "provincia": "Santa Fe",
        "ciudad": "Rosario"
    },
    "Sur (Centro 0A16)": {
        "codigos": ["0A16", "A16", "0a16", "a16"], # Agregamos minúsculas por si SAP lo exporta distinto
        "depot_address": "Depósito Base Sur, Bahía Blanca, Buenos Aires, Argentina",
        "depot_coords": (-38.7183, -62.2663), 
        "provincia": "Buenos Aires",
        "ciudad": "Bahía Blanca"
    },
    "Cuyo Sur (Centro 0A22)": {
        "codigos": ["0A22", "A22", "0a22", "a22"],
        "depot_address": "Depósito Base Cuyo Sur, San Juan Capital, San Juan, Argentina",
        "depot_coords": (-31.5375, -68.5363), 
        "provincia": "San Juan",
        "ciudad": "San Juan Capital"
    },
    "Patagonia (Centro 0A36)": {
        "codigos": ["0A36", "A36", "0a36", "a36"],
        "depot_address": "Depósito Base Patagonia, Trelew, Chubut, Argentina",
        "depot_coords": (-43.2489, -65.3050), 
        "provincia": "Chubut",
        "ciudad": "Trelew"
    }
}

# ==========================================
# CARGA DE DICCIONARIO DESDE ARCHIVO CSV
# ==========================================
@st.cache_data
def cargar_diccionario_cp():
    try:
        # Leemos el CSV asegurando que todo se tome como texto
        df_cp = pd.read_csv("codigos_postales.csv", dtype=str)
        
        # Estandarizamos los nombres de las columnas (minúsculas y sin espacios) por si vienen raros
        df_cp.columns = [str(c).replace('"', '').strip().lower() for c in df_cp.columns]
        
        # Verificamos que estén las columnas clave que mencionaste
        if 'cp' in df_cp.columns and 'localidad' in df_cp.columns and 'provincia' in df_cp.columns:
            
            # Limpiamos los datos y armamos la estructura "Localidad, Provincia"
            df_cp['cp'] = df_cp['cp'].str.strip()
            df_cp['ubicacion_google'] = df_cp['localidad'].str.strip() + ", " + df_cp['provincia'].str.strip()
            
            # Borramos los CP que puedan estar vacíos por error en la base
            df_cp = df_cp.dropna(subset=['cp'])
            
            # Armamos y devolvemos el diccionario dinámico
            return dict(zip(df_cp['cp'], df_cp['ubicacion_google']))
            
        else:
            st.warning("⚠️ El archivo CSV no tiene las columnas exactas (cp, localidad, provincia).")
            return {}
            
    except Exception as e:
        st.warning(f"⚠️ Error al leer codigos_postales.csv: {e}")
        return {}

DICCIONARIO_CP = cargar_diccionario_cp()
# ==========================================

MINUTOS_POR_PARADA = 25
MAX_HORAS_JORNADA = 7.5

st.title("🚚 Torre de Control - Servicio Técnico Frío")
st.subheader("Ruteador Multirregión (Geolocalización Inteligente)")

st.sidebar.header("⚙️ Configuración")
google_api_key = st.sidebar.text_input("Ingrese Google Maps API Key:", type="password")

if st.sidebar.button("🔄 Actualizar base de Códigos Postales"):
    st.cache_data.clear()
    st.rerun()

if not google_api_key:
    st.info("👈 **Para comenzar, ingresá tu Google Maps API Key en el menú lateral izquierdo.**")
    st.stop()

try:
    gmaps = googlemaps.Client(key=google_api_key)
except Exception as e:
    st.error(f"Error al conectar con la API de Google Maps: {e}")
    st.stop()

uploaded_file = st.file_uploader("Cargar planilla de órdenes (.xlsx)", type=["xlsx"])

if uploaded_file is not None:
    try:
        df = pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Error al leer el archivo Excel: {e}")
        st.stop()

    df.columns = [str(c).strip() for c in df.columns]

    # Mapeo de columnas
    col_orden = df.columns[0]
    col_cliente = df.columns[2]
    col_direccion = df.columns[3]
    col_telefono = df.columns[5]
    col_cp = df.columns[6]
    col_texto_breve = df.columns[7]
    col_puesto = df.columns[8]      # Col I: Puesto de trabajo
    col_activo = df.columns[9]
    col_centro = df.columns[10]

    opcion_region = st.selectbox("Seleccione el Centro / Región a procesar:", list(CENTROS_CONFIG.keys()))
    config_actual = CENTROS_CONFIG[opcion_region]

    codigos_centro = [str(c) for c in config_actual["codigos"]]
    df_filtrado = df[df[col_centro].astype(str).str.strip().isin(codigos_centro)].copy()

    if df_filtrado.empty:
        st.warning(f"⚠️ No se encontraron órdenes correspondientes a {opcion_region} en este archivo.")
        st.stop()

    st.success(f"📊 Se encontraron **{len(df_filtrado)} órdenes en total** asociadas a **{opcion_region}**.")

    # ==========================================
    # NUEVO: ALERTA DE CÓDIGOS POSTALES FALTANTES
    # ==========================================
    cps_en_ordenes = df_filtrado[col_cp].apply(lambda x: str(x).replace('.0', '').strip() if pd.notna(x) else "").unique()
    cps_faltantes = [cp for cp in cps_en_ordenes if cp and cp not in DICCIONARIO_CP]

    if cps_faltantes:
        st.warning(
            f"🚨 **¡ALERTA DE CÓDIGOS POSTALES NO REGISTRADOS!**\n\n"
            f"Se detectaron **{len(cps_faltantes)} Código(s) Postal(es)** en esta planilla que no están en tu archivo maestro de GitHub:\n\n"
            f"👉 **{', '.join(cps_faltantes)}**\n\n"
            f"⚠️ *El sistema intentará ubicarlos usando la ciudad base ({config_actual['ciudad']}), pero si son del interior, probablemente requieran revisión manual en el paso siguiente. Acordate de agregarlos a tu CSV de GitHub para la próxima.*"
        )
        st.divider()
    # ==========================================

    def obtener_localidad(cp_val):
        cp_limpio = str(cp_val).replace('.0', '').strip() if pd.notna(cp_val) else ""
        return DICCIONARIO_CP.get(cp_limpio, f"{config_actual['ciudad']}, {config_actual['provincia']}")

    df_filtrado['Localidad_Mapeada'] = df_filtrado[col_cp].apply(obtener_localidad)

    st.divider()
    st.markdown("### 🌍 Filtro de Localidades (Zonificación)")
    
    localidades_presentes = sorted(df_filtrado['Localidad_Mapeada'].unique())
    
    localidades_seleccionadas = st.multiselect(
        "Seleccioná qué zonas querés planificar en esta tanda de ruteo:",
        options=localidades_presentes,
        default=localidades_presentes
    )
    
    if not localidades_seleccionadas:
        st.info("👆 Seleccioná al menos una zona para continuar con la validación de mapas.")
        st.stop()
        
    df_filtrado = df_filtrado[df_filtrado['Localidad_Mapeada'].isin(localidades_seleccionadas)].copy()
    
    def limpiar_direccion(dir_raw):
        if pd.isna(dir_raw):
            return ""
        dir_str = str(dir_raw).replace('\n', ' ').replace('\r', ' ').strip().upper()
        dir_str = re.sub(r'\b0+(\d+)', r'\1', dir_str)
        dir_str = dir_str.replace('.', ' ').replace(',', ' ').replace('-', ' ')
        patron_basura = r'\b(PISO|PB|DPTO|DPT|DEPTO|OFICINA|OF|LOTE|MZ|MANZANA|LOCAL|BARRIO|B°|B )\b.*'
        dir_str = re.sub(patron_basura, '', dir_str)
        dir_str = re.sub(r'\s+', ' ', dir_str).strip()
        return dir_str

    df_filtrado['direccion_limpia'] = df_filtrado[col_direccion].apply(limpiar_direccion)

    def geocodificar_google(dir_texto, ubicacion_geografica):
        if "S/N" in dir_texto.upper() or "S/ N" in dir_texto.upper():
            return config_actual["depot_coords"][0], config_actual["depot_coords"][1], "Falta altura exacta (S/N)", False
        
        query_principal = f"{dir_texto}, {ubicacion_geografica}, Argentina"
        
        try:
            res = gmaps.geocode(query_principal, region='ar', components={"country": "AR"})
            if res and len(res) > 0:
                location = res[0]['geometry']['location']
                formatted_address = res[0].get('formatted_address', dir_texto)
                return location['lat'], location['lng'], formatted_address, True
            else:
                return config_actual["depot_coords"][0], config_actual["depot_coords"][1], f"Google no ubicó: {query_principal}", False
        except Exception as e:
            return config_actual["depot_coords"][0], config_actual["depot_coords"][1], f"ERROR API: {str(e)}", False

    if "coords_cache_gmaps" not in st.session_state:
        st.session_state.coords_cache_gmaps = {}
    if "direcciones_editadas" not in st.session_state:
        st.session_state.direcciones_editadas = {}
    if "direcciones_descartadas" not in st.session_state:
        st.session_state.direcciones_descartadas = set()

    st.divider()
    c_tit, c_btn = st.columns([3, 1])
    c_tit.markdown("### 🔍 Validación con Google Maps")
    
    if c_btn.button("🧹 Limpiar Memoria y Reintentar"):
        st.session_state.coords_cache_gmaps = {}
        st.rerun()

    direcciones_unicas = df_filtrado[['direccion_limpia', 'Localidad_Mapeada']].drop_duplicates()
    no_encontradas = []

    with st.spinner("Consultando a Google Maps..."):
        for _, row_dir in direcciones_unicas.iterrows():
            d_orig = row_dir['direccion_limpia']
            loc_map = row_dir['Localidad_Mapeada']
            
            if d_orig in st.session_state.direcciones_descartadas:
                continue

            d_actual = st.session_state.direcciones_editadas.get(d_orig, d_orig)
            cache_key = f"{d_actual}_{loc_map}"

            if cache_key not in st.session_state.coords_cache_gmaps:
                lat, lng, formatted_addr, exito = geocodificar_google(d_actual, loc_map)
                st.session_state.coords_cache_gmaps[cache_key] = (lat, lng, formatted_addr, exito)
            
            lat, lng, formatted_addr, exito = st.session_state.coords_cache_gmaps[cache_key]
            
            if not exito:
                no_encontradas.append((d_orig, d_actual, formatted_addr))

    df_filtrado_activo = df_filtrado[~df_filtrado['direccion_limpia'].isin(st.session_state.direcciones_descartadas)].copy()

    if no_encontradas:
        st.warning(f"⚠️ **Atención:** Hay **{len(no_encontradas)} dirección(es)** que requieren revisión manual:")
        for d_orig, d_actual, error_msg in no_encontradas:
            with st.container():
                st.caption(f"🛑 **Respuesta de Google:** {error_msg}")
                c1, c2, c3 = st.columns([3, 1, 1])
                with c1:
                    nueva_dir = st.text_input("Dirección:", value=d_actual, key=f"input_{d_orig}")
                with c2:
                    st.write(" ")
                    st.write(" ")
                    if st.button("🔄 Recalcular", key=f"recalc_{d_orig}"):
                        st.session_state.direcciones_editadas[d_orig] = nueva_dir
                        for k in list(st.session_state.coords_cache_gmaps.keys()):
                            if d_orig in k or d_actual in k:
                                del st.session_state.coords_cache_gmaps[k]
                        st.rerun()
                with c3:
                    st.write(" ")
                    st.write(" ")
                    if st.button("❌ Descartar", key=f"discard_{d_orig}"):
                        st.session_state.direcciones_descartadas.add(d_orig)
                        st.rerun()
                st.divider()
    else:
        st.success("✅ ¡Google Maps ubicó correctamente todas las direcciones de la zona seleccionada!")

    # ==========================================
    # PLANIFICACIÓN DE FLOTA (VEHÍCULOS)
    # ==========================================
    st.divider()
    st.markdown("### 🚗 Planificación de Flota")
    
    total_direcciones_unicas = len(df_filtrado_activo['direccion_limpia'].unique())
    
    sug_vehiculos = 1
    if total_direcciones_unicas > 8: sug_vehiculos = 2
    if total_direcciones_unicas > 16: sug_vehiculos = 3
    
    col_v1, col_v2 = st.columns([2, 2])
    with col_v1:
        cant_vehiculos = st.number_input(
            "Seleccioná la cantidad de vehículos para esta ruta:",
            min_value=1,
            max_value=10,
            value=sug_vehiculos,
            help="El sistema agrupará los locales automáticamente en la cantidad de vehículos que elijas."
        )
    with col_v2:
        st.info(f"💡 **Sugerencia:** Para **{total_direcciones_unicas} ubicaciones**, recomendamos despachar **{sug_vehiculos} vehículo(s)**.")

    if df_filtrado_activo.empty:
        st.error("No hay direcciones válidas para procesar el ruteo.")
        st.stop()

    # --- 1. AGRUPACIÓN AUTOMÁTICA (K-MEANS) ---
    grupos_locales = []
    for (dir_orig, loc_map), group in df_filtrado_activo.groupby(['direccion_limpia', 'Localidad_Mapeada']):
        dir_final = st.session_state.direcciones_editadas.get(dir_orig, dir_orig)
        cache_key = f"{dir_final}_{loc_map}"
        
        if cache_key in st.session_state.coords_cache_gmaps:
            lat, lng, formatted_addr, _ = st.session_state.coords_cache_gmaps[cache_key]
        else:
            lat, lng, formatted_addr = config_actual["depot_coords"][0], config_actual["depot_coords"][1], dir_orig

        detalles_ordenes = []
        for _, row in group.iterrows():
            detalles_ordenes.append({
                'orden': str(row[col_orden]),
                'cliente': str(row[col_cliente]),
                'tel': str(row[col_telefono]),
                'activo': str(row[col_activo]),
                'obs': str(row[col_texto_breve]),
                'puesto': str(row[col_puesto])
            })
        
        grupos_locales.append({
            'direccion': formatted_addr,
            'cliente_principal': group[col_cliente].iloc[0],
            'lat': lat,
            'lng': lng,
            'cant_ordenes': len(group),
            'detalles': detalles_ordenes,
            'indices_originales': group.index.tolist()
        })

    df_locales = pd.DataFrame(grupos_locales)
    depot_lat, depot_lng = config_actual["depot_coords"]

    if cant_vehiculos > 1 and len(df_locales) >= cant_vehiculos:
        coords_matrix = df_locales[['lat', 'lng']].values
        kmeans = KMeans(n_clusters=cant_vehiculos, random_state=42, n_init=10).fit(coords_matrix)
        df_locales['cluster'] = kmeans.labels_
        vehiculo_map = {i: f'Vehículo {i+1}' for i in range(cant_vehiculos)}
        df_locales['Vehículo Asignado'] = df_locales['cluster'].map(vehiculo_map)
    else:
        df_locales['Vehículo Asignado'] = 'Vehículo 1'

    # --- 2. TABLA INTERACTIVA DE AJUSTE MANUAL ---
    st.markdown("#### 🔀 Revisión y Ajuste de Zonas")
    st.caption("El algoritmo agrupa matemáticamente por cercanía. Si querés forzar un cambio, **hacé clic en la columna 'Vehículo Asignado' y modificalo a tu gusto** antes de calcular las rutas.")

    opciones_vehiculos = [f"Vehículo {i+1}" for i in range(cant_vehiculos)]
    
    df_para_editar = df_locales[['cliente_principal', 'direccion', 'cant_ordenes', 'Vehículo Asignado']].copy()
    
    df_editado = st.data_editor(
        df_para_editar,
        column_config={
            "cliente_principal": st.column_config.TextColumn("Cliente", disabled=True),
            "direccion": st.column_config.TextColumn("Ubicación", disabled=True),
            "cant_ordenes": st.column_config.NumberColumn("Órdenes", disabled=True),
            "Vehículo Asignado": st.column_config.SelectboxColumn(
                "Vehículo Asignado (Editable)", 
                help="Clickeá para asignar este cliente a otro vehículo",
                width="medium",
                options=opciones_vehiculos, 
                required=True
            )
        },
        hide_index=True,
        use_container_width=True,
        key="tabla_ajuste_vehiculos"
    )

    # Actualizamos el dataframe maestro con las decisiones manuales
    df_locales['Vehículo Asignado'] = df_editado['Vehículo Asignado']

    # --- 3. BOTÓN FINAL: OPTIMIZAR SECUENCIA (OR-TOOLS) ---
    st.divider()
    if st.button("⚡ Calcular Secuencias Óptimas y Despachar", type="primary"):
        with st.spinner("Trazando las rutas para los vehículos configurados..."):
            
            def optimizar_secuencia_grupo(df_grupo):
                coords_grupo = [(depot_lat, depot_lng)]
                for _, row in df_grupo.iterrows():
                    coords_grupo.append((row['lat'], row['lng']))

                n_locs = len(coords_grupo)
                dist_matrix = []

                for i in range(n_locs):
                    row_dist = []
                    for j in range(n_locs):
                        if i == j:
                            row_dist.append(0)
                        else:
                            lat1, lon1 = np.radians(coords_grupo[i][0]), np.radians(coords_grupo[i][1])
                            lat2, lon2 = np.radians(coords_grupo[j][0]), np.radians(coords_grupo[j][1])
                            dlat, dlon = lat2 - lat1, lon2 - lon1
                            a = np.sin(dlat/2)**2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon/2)**2
                            c = 2 * np.arcsin(np.sqrt(a))
                            dist_meters = int(c * 6371000 * 1.35)
                            row_dist.append(dist_meters)
                    dist_matrix.append(row_dist)

                manager = pywrapcp.RoutingIndexManager(n_locs, 1, 0)
                routing = pywrapcp.RoutingModel(manager)

                def distance_callback(from_index, to_index):
                    from_node = manager.IndexToNode(from_index)
                    to_node = manager.IndexToNode(to_index)
                    return int(dist_matrix[from_node][to_node])

                transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

                search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

                solution = routing.SolveWithParameters(search_parameters)
                secuencia_indices, distancia_total = [], 0

                if solution:
                    index = routing.Start(0)
                    while not routing.IsEnd(index):
                        node = manager.IndexToNode(index)
                        if node != 0:
                            secuencia_indices.append(node - 1)
                        prev_index = index
                        index = solution.Value(routing.NextVar(index))
                        distancia_total += routing.GetArcCostForVehicle(prev_index, index, 0)

                return df_grupo.iloc[secuencia_indices].copy(), distancia_total / 1000.0

            vehiculos_unicos = df_locales['Vehículo Asignado'].unique()

            st.info(f"💡 **DESPACHO FINAL:** Se asignaron **{len(vehiculos_unicos)} VEHÍCULO(S)** saliendo de `{config_actual['depot_address']}`.")

            cols = st.columns(len(vehiculos_unicos))
            
            for idx_col, v_nombre in enumerate(sorted(vehiculos_unicos)):
                grupo_df = df_locales[df_locales['Vehículo Asignado'] == v_nombre]
                
                sub_df_ordenado, km_v = optimizar_secuencia_grupo(grupo_df)
                
                direcciones_ordenadas = sub_df_ordenado['direccion'].tolist()
                
                rutas_links = []
                tamano_bloque = 9
                
                for i in range(0, len(direcciones_ordenadas), tamano_bloque):
                    bloque = direcciones_ordenadas[i:i+tamano_bloque]
                    
                    if i == 0:
                        origen_ruta = config_actual["depot_address"]
                    else:
                        origen_ruta = direcciones_ordenadas[i-1]
                        
                    if i + tamano_bloque >= len(direcciones_ordenadas):
                        destino_ruta = config_actual["depot_address"]
                    else:
                        destino_ruta = bloque[-1]
                        bloque = bloque[:-1]
                        
                    waypoints_str = "|".join([urllib.parse.quote(d) for d in bloque])
                    origen_enc = urllib.parse.quote(origen_ruta)
                    destino_enc = urllib.parse.quote(destino_ruta)
                    
                    link = f"https://www.google.com/maps/dir/?api=1&origin={origen_enc}&destination={destino_enc}&waypoints={waypoints_str}&travelmode=driving"
                    rutas_links.append(link)
                
                tiempo_est = (km_v / 25.0) + ((len(sub_df_ordenado) * MINUTOS_POR_PARADA) / 60.0)

                with cols[idx_col]:
                    st.markdown(f"### 🚚 {v_nombre}")
                    st.metric("Ubicaciones a Visitar", len(sub_df_ordenado))
                    st.metric("Recorrido Est.", f"{km_v:.1f} km")
                    st.metric("Jornada Est.", f"{tiempo_est:.1f} hs")

                    st.markdown("**Secuencia Óptima de Paradas:**")
                    paso = 1
                    texto_paradas_wa = ""
                    
                    for _, local in sub_df_ordenado.iterrows():
                        cant_ord = local['cant_ordenes']
                        
                        st.markdown(f"**{paso}. {local['cliente_principal']}**")
                        st.caption(f"📍 {local['direccion']}")
                        
                        texto_paradas_wa += f"%0A*{paso}. {local['cliente_principal']}*%0A"
                        texto_paradas_wa += f"📍 {local['direccion']}%0A"

                        for det in local['detalles']:
                            st.write(
                                f"↳ Puesto: `{det['puesto']}` | Orden: `{det['orden']}` | "
                                f"Activo: `{det['activo']}` | Obs: `{det['obs']}`"
                            )
                            
                            texto_paradas_wa += (
                                f"   🔸 *Puesto de trabajo:* {det['puesto']}%0A"
                                f"   🔸 *N° Orden:* {det['orden']}%0A"
                                f"   🔸 *Activo Fijo:* {det['activo']}%0A"
                                f"   🔸 *Observación:* {det['obs']}%0A%0A"
                            )

                        for orig_idx in local['indices_originales']:
                            df.loc[orig_idx, 'Vehículo Asignado'] = v_nombre
                            df.loc[orig_idx, 'Estado'] = 'En Ruta'
                            df.loc[orig_idx, 'Link de Ruta'] = str(rutas_links[0])

                        st.write("---")
                        paso += 1

                    for idx_link, link_ruta in enumerate(rutas_links):
                        nombre_boton = "🗺️ Abrir Hoja de Ruta Completa" if len(rutas_links) == 1 else f"🗺️ Abrir Ruta (Parte {idx_link + 1})"
                        st.link_button(nombre_boton, link_ruta)
                    
                    texto_links_wa = ""
                    if len(rutas_links) == 1:
                        texto_links_wa = f"🔗 *Link de Ruta Google Maps:*%0A{urllib.parse.quote(rutas_links[0])}"
                    else:
                        texto_links_wa = "🔗 *Links de Ruta (Dividida por límite de Google Maps):*%0A"
                        for idx_link, link_ruta in enumerate(rutas_links):
                            texto_links_wa += f"📍 *Parte {idx_link + 1}:*%0A{urllib.parse.quote(link_ruta)}%0A%0A"
                    
                    msg_wa = (
                        f"🚚 *HOJA DE RUTA - {v_nombre.upper()}*%0A"
                        f"📊 *Ubicaciones a Visitar:* {len(sub_df_ordenado)} puntos%0A"
                        f"----------------------------------------%0A"
                        f"📋 *DETALLE DE PUNTOS Y ACTIVOS:*%0A"
                        f"{texto_paradas_wa}"
                        f"----------------------------------------%0A"
                        f"{texto_links_wa}"
                    )
                    
                    st.link_button("💬 Enviar por WhatsApp", f"https://api.whatsapp.com/send?text={msg_wa}")

            # Botón de Descargar Excel
            st.divider()
            output_name = "ordenes_despachadas.xlsx"
            df.to_excel(output_name, index=False)
            
            with open(output_name, "rb") as file:
                st.download_button(
                    label="📥 Descargar Excel con Asignaciones y Links",
                    data=file,
                    file_name="ordenes_despachadas.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
