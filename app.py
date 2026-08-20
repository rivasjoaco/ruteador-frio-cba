import streamlit as st
import pandas as pd
import numpy as np
import urllib.parse
import re
import unicodedata
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
        "codigos": ["0A16", "A16", "0a16", "a16"], 
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
        df_cp = pd.read_csv("codigos_postales.csv", dtype=str)
        df_cp.columns = [str(c).replace('"', '').strip().lower() for c in df_cp.columns]
        if 'cp' in df_cp.columns and 'localidad' in df_cp.columns and 'provincia' in df_cp.columns:
            df_cp['cp'] = df_cp['cp'].str.strip()
            df_cp['ubicacion_google'] = df_cp['localidad'].str.strip() + ", " + df_cp['provincia'].str.strip()
            df_cp = df_cp.dropna(subset=['cp'])
            return dict(zip(df_cp['cp'], df_cp['ubicacion_google']))
        else:
            return {}
    except Exception:
        return {}

DICCIONARIO_CP = cargar_diccionario_cp()

# ==========================================
# CARGA DE MAESTRO DE CLIENTES
# ==========================================
@st.cache_data
def cargar_maestro_clientes():
    try:
        df_maestro = pd.read_csv("maestro_clientes.csv", dtype=str, sep=";", on_bad_lines='skip')
        df_maestro.columns = [str(c).strip() for c in df_maestro.columns]
        return df_maestro
    except Exception as e:
        st.warning(f"⚠️ No se encontró maestro_clientes.csv: {e}")
        return pd.DataFrame()

DF_MAESTRO = cargar_maestro_clientes()

MINUTOS_POR_PARADA = 25

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
    col_orden = df.columns[0]      # A
    col_cliente = df.columns[1]    # B
    col_direccion = df.columns[3]  # D
    col_telefono = df.columns[5]   # F
    col_cp = df.columns[6]         # G
    col_texto_breve = df.columns[7]# H
    col_puesto = df.columns[8]     # I
    col_activo = df.columns[9]     # J
    col_centro = df.columns[10]    # K
    col_fecha = df.columns[11]     # L (Fecha creación)

    opcion_region = st.selectbox("Seleccione el Centro / Región a procesar:", list(CENTROS_CONFIG.keys()))
    config_actual = CENTROS_CONFIG[opcion_region]

    st.markdown("#### 📍 Origen y Destino de la Flota")
    origen_personalizado = st.text_input(
        "Dirección de salida y regreso (Editable):", 
        value=config_actual["depot_address"]
    )

    codigos_centro = [str(c) for c in config_actual["codigos"]]
    df_filtrado = df[df[col_centro].astype(str).str.strip().isin(codigos_centro)].copy()

    if df_filtrado.empty:
        st.warning(f"⚠️ No se encontraron órdenes correspondientes a {opcion_region}.")
        st.stop()

    # --- CÁLCULO DE ANTIGÜEDAD DE LA ORDEN ---
    df_filtrado[col_fecha] = pd.to_datetime(df_filtrado[col_fecha], errors='coerce')
    hoy = pd.Timestamp.today()
    df_filtrado['Dias_Pendiente'] = (hoy - df_filtrado[col_fecha]).dt.days.fillna(0).astype(int)
    
    # Creamos la alerta visual
    df_filtrado['Alerta_Fecha'] = df_filtrado['Dias_Pendiente'].apply(
        lambda x: f"🚨 {x} días" if x >= 3 else f"🟢 {x} días" if x == 0 else f"🟡 {x} días"
    )

    # ==========================================
    # CRUCE CON BASE MAESTRA
    # ==========================================
    if not DF_MAESTRO.empty and 'Cliente' in DF_MAESTRO.columns:
        df_filtrado[col_cliente] = (
            df_filtrado[col_cliente]
            .astype(str)
            .str.replace('.0', '', regex=False)
            .str.strip()
            .str.zfill(9) 
        )
        DF_MAESTRO['Cliente'] = (
            DF_MAESTRO['Cliente']
            .astype(str)
            .str.replace('.0', '', regex=False)
            .str.strip()
            .str.zfill(9) 
        )
        
        df_filtrado = df_filtrado.merge(
            DF_MAESTRO[['Cliente', 'Zona de Venta', 'Latitud', 'Longitud', 'Nombre Fantasía', 'Entre Calles', 'Dirección']],
            left_on=col_cliente,
            right_on='Cliente',
            how='left'
        )
        
        df_filtrado['Nombre Fantasía'] = df_filtrado['Nombre Fantasía'].fillna('')
        df_filtrado['Entre Calles'] = df_filtrado['Entre Calles'].fillna('')
        df_filtrado['Direccion_Maestro'] = df_filtrado['Dirección'].fillna('')
    else:
        df_filtrado['Zona de Venta'] = "Sin Zona"
        df_filtrado['Latitud'] = np.nan
        df_filtrado['Longitud'] = np.nan
        df_filtrado['Direccion_Maestro'] = ""

    df_filtrado['Zona de Venta'] = df_filtrado['Zona de Venta'].fillna('ZONA DESCONOCIDA')

    def obtener_localidad(cp_val):
        cp_limpio = str(cp_val).replace('.0', '').strip() if pd.notna(cp_val) else ""
        return DICCIONARIO_CP.get(cp_limpio, f"{config_actual['ciudad']}, {config_actual['provincia']}")
    
    df_filtrado['Localidad_Mapeada'] = df_filtrado[col_cp].apply(obtener_localidad)

    # ==========================================
    # FILTRO OPERATIVO Y MANUAL
    # ==========================================
    st.divider()
    st.markdown("### 🌍 Filtro Operativo (Zonas de Venta)")
    
    zonas_presentes = sorted(df_filtrado['Zona de Venta'].unique())
    zonas_seleccionadas = st.multiselect(
        "Seleccioná qué Zonas de Venta querés rutear hoy:",
        options=zonas_presentes,
        default=zonas_presentes
    )
    
    if not zonas_seleccionadas:
        st.stop()
        
    df_filtrado = df_filtrado[df_filtrado['Zona de Venta'].isin(zonas_seleccionadas)].copy()

    st.markdown("#### 🚫 Selección de Viajes Específicos")
    df_filtrado.insert(0, "Incluir", True)
    
    df_seleccion_ordenes = st.data_editor(
        df_filtrado[['Incluir', 'Alerta_Fecha', col_orden, col_cliente, col_direccion, 'Zona de Venta']],
        column_config={
            "Incluir": st.column_config.CheckboxColumn("¿Rutear?", default=True),
            "Alerta_Fecha": st.column_config.TextColumn("Demora", disabled=True),
            col_orden: st.column_config.TextColumn("N° Orden", disabled=True),
            col_cliente: st.column_config.TextColumn("Cliente", disabled=True),
            col_direccion: st.column_config.TextColumn("Dirección", disabled=True),
            "Zona de Venta": st.column_config.TextColumn("Zona", disabled=True)
        },
        hide_index=True,
        use_container_width=True,
        key="filtro_ordenes_manual"
    )
    
    ordenes_seleccionadas = df_seleccion_ordenes[df_seleccion_ordenes['Incluir'] == True][col_orden].tolist()
    df_filtrado = df_filtrado[df_filtrado[col_orden].isin(ordenes_seleccionadas)].copy()
    
    if df_filtrado.empty:
        st.stop()

    def limpiar_direccion(dir_raw):
        if pd.isna(dir_raw): return ""
        dir_str = str(dir_raw).replace('\n', ' ').replace('\r', ' ').strip().upper()
        dir_str = ''.join(c for c in unicodedata.normalize('NFD', dir_str) if unicodedata.category(c) != 'Mn')
        dir_str = re.sub(r'\b0+(\d+)', r'\1', dir_str)
        dir_str = dir_str.replace('.', ' ').replace(',', ' ').replace('-', ' ')
        dir_str = re.sub(r'\b(PISO|PB|DPTO|DPT|DEPTO|OFICINA|OF|LOTE|MZ|MANZANA|LOCAL|BARRIO|B°|B )\b.*', '', dir_str)
        return re.sub(r'\s+', ' ', dir_str).strip()

    df_filtrado['direccion_limpia'] = df_filtrado[col_direccion].apply(limpiar_direccion)
    df_filtrado['dir_maestro_limpia'] = df_filtrado['Direccion_Maestro'].apply(limpiar_direccion)

    def geocodificar_google(dir_texto, ubicacion_geografica):
        query_principal = f"{dir_texto}, {ubicacion_geografica}, Argentina"
        try:
            res = gmaps.geocode(query_principal, region='ar', components={"country": "AR"})
            if res and len(res) > 0:
                loc = res[0]['geometry']['location']
                return loc['lat'], loc['lng'], res[0].get('formatted_address', dir_texto), True
            return config_actual["depot_coords"][0], config_actual["depot_coords"][1], f"No ubicado", False
        except Exception:
            return config_actual["depot_coords"][0], config_actual["depot_coords"][1], f"ERROR API", False

    if "coords_cache" not in st.session_state: st.session_state.coords_cache = {}
    if "direcciones_descartadas" not in st.session_state: st.session_state.direcciones_descartadas = set()

    # ==========================================
    # VALIDACIÓN DE COORDENADAS
    # ==========================================
    st.divider()
    c_tit, c_btn = st.columns([3, 1])
    c_tit.markdown("### 🔍 Validación de Coordenadas (Modo Estricto)")
    if c_btn.button("🧹 Limpiar Memoria"):
        st.session_state.coords_cache = {}
        st.rerun()

    direcciones_unicas = df_filtrado[['direccion_limpia', 'dir_maestro_limpia', 'Localidad_Mapeada', 'Latitud', 'Longitud', col_cliente]].drop_duplicates(subset=[col_cliente, 'direccion_limpia'])
    no_encontradas = []

    with st.spinner("Buscando y comparando sucursales con Base Maestra..."):
        for _, row_dir in direcciones_unicas.iterrows():
            d_orig = row_dir['direccion_limpia']
            d_maestro = row_dir['dir_maestro_limpia']
            loc_map = row_dir['Localidad_Mapeada']
            c_id = row_dir[col_cliente]
            
            if d_orig in st.session_state.direcciones_descartadas: continue
            
            cache_key = f"{c_id}_{d_orig}"

            if cache_key not in st.session_state.coords_cache:
                lat_m = row_dir['Latitud']
                lng_m = row_dir['Longitud']
                exito = False
                motivo_error = "Faltan coordenadas en el Maestro"

                es_casa_central = False
                if d_orig == d_maestro:
                    es_casa_central = True
                else:
                    pal_orig = [p for p in d_orig.split() if len(p) > 2 and not p.isdigit()]
                    pal_mae = [p for p in d_maestro.split() if len(p) > 2 and not p.isdigit()]
                    if pal_orig and pal_mae and pal_orig[0] == pal_mae[0]:
                        es_casa_central = True
                
                if not d_maestro: es_casa_central = True

                if es_casa_central:
                    if pd.notna(lat_m) and pd.notna(lng_m) and str(lat_m).strip() != "":
                        try:
                            def limpiar_coord(val):
                                s = str(val).strip()
                                s = re.sub(r'[^\d-]', '', s)
                                if not s or s == '-': return None
                                if not s.startswith('-'): s = '-' + s
                                if len(s) > 3: s = s[:3] + '.' + s[3:] 
                                return float(s)
                            
                            lng_val = limpiar_coord(lat_m)
                            lat_val = limpiar_coord(lng_m)
                            
                            st.session_state.coords_cache[cache_key] = (lat_val, lng_val, f"{d_orig} (Base Maestra)", True)
                            exito = True
                        except:
                            motivo_error = "Error al leer lat/lng del Maestro"
                else:
                    motivo_error = f"Sucursal Diferente (Maestro indica: '{d_maestro}')"

                if not exito:
                    st.session_state.coords_cache[cache_key] = (0, 0, motivo_error, False)
            
            lat, lng, msg, exito = st.session_state.coords_cache[cache_key]
            if not exito: no_encontradas.append((d_orig, c_id, msg, loc_map, cache_key))

    df_filtrado_activo = df_filtrado[~df_filtrado['direccion_limpia'].isin(st.session_state.direcciones_descartadas)].copy()

    if no_encontradas:
        st.error(f"🚨 **¡ATENCIÓN!** Hay **{len(no_encontradas)} cliente(s)** con conflictos (Sin coordenadas o Sucursales Nuevas):")
        st.info("💡 **Acción requerida:** Podés autorizar a buscar la sucursal en Google Maps o descartar la orden.")
        for d_orig, c_id, msg, loc_map, c_key in no_encontradas:
            with st.container():
                c1, c2, c3 = st.columns([3, 1, 1])
                with c1: 
                    st.warning(f"🛑 Cliente Nº **{c_id}** - Dirección: `{d_orig}`\n\n*Motivo:* {msg}")
                with c2:
                    st.write(" ")
                    if st.button("🔍 Forzar Google Maps", key=f"google_{c_key}"):
                        lat, lng, f_addr, ok = geocodificar_google(d_orig, loc_map)
                        if ok:
                            st.session_state.coords_cache[c_key] = (lat, lng, f_addr, True)
                        else:
                            st.session_state.coords_cache[c_key] = (lat, lng, "Google no lo encontró", False)
                        st.rerun()
                with c3:
                    st.write(" ")
                    if st.button("❌ Descartar", key=f"discard_{d_orig}"):
                        st.session_state.direcciones_descartadas.add(d_orig)
                        st.rerun()
                st.write("---")
        st.stop()
    else:
        st.success("✅ ¡El 100% de las ubicaciones fueron validadas y analizadas!")

    # ==========================================
    # PLANIFICACIÓN DE FLOTA
    # ==========================================
    st.divider()
    st.markdown("### 🚗 Planificación de Flota")
    
    total_direcciones_unicas = len(df_filtrado_activo[['direccion_limpia', col_cliente]].drop_duplicates())
    
    sug_vehiculos = 1
    if total_direcciones_unicas > 8: sug_vehiculos = 2
    if total_direcciones_unicas > 16: sug_vehiculos = 3
    
    col_v1, col_v2 = st.columns([2, 2])
    with col_v1:
        cant_vehiculos = st.number_input(
            "Seleccioná la cantidad de vehículos para esta ruta:",
            min_value=1,
            max_value=10,
            value=sug_vehiculos
        )
    with col_v2:
        st.info(f"💡 **Sugerencia:** Para **{total_direcciones_unicas} paradas**, recomendamos despachar **{sug_vehiculos} vehículo(s)**.")

    if df_filtrado_activo.empty:
        st.stop()

    grupos_locales = []
    
    for (cliente_id, dir_orig), group in df_filtrado_activo.groupby([col_cliente, 'direccion_limpia']):
        cache_key = f"{cliente_id}_{dir_orig}"
        
        if cache_key in st.session_state.coords_cache:
            lat, lng, formatted_addr, _ = st.session_state.coords_cache[cache_key]
        else:
            lat, lng, formatted_addr = config_actual["depot_coords"][0], config_actual["depot_coords"][1], dir_orig

        max_dias = group['Dias_Pendiente'].max()
        
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
            'cliente_principal': cliente_id,
            'nombre_fantasia': group['Nombre Fantasía'].iloc[0],
            'entre_calles': group['Entre Calles'].iloc[0],
            'lat': lat,
            'lng': lng,
            'max_dias': max_dias,
            'Forzar_Primera': False,
            'cant_ordenes': len(group),
            'detalles': detalles_ordenes,
            'indices_originales': group.index.tolist()
        })

    df_locales = pd.DataFrame(grupos_locales)

    if cant_vehiculos > 1 and len(df_locales) >= cant_vehiculos:
        coords_matrix = df_locales[['lat', 'lng']].values
        kmeans = KMeans(n_clusters=cant_vehiculos, random_state=42, n_init=10).fit(coords_matrix)
        df_locales['cluster'] = kmeans.labels_
        vehiculo_map = {i: f'Vehículo {i+1}' for i in range(cant_vehiculos)}
        df_locales['Vehículo Asignado'] = df_locales['cluster'].map(vehiculo_map)
    else:
        df_locales['Vehículo Asignado'] = 'Vehículo 1'

    # --- 2. TABLA INTERACTIVA (AHORA CON BOTÓN VIP DE PRIORIDAD) ---
    st.markdown("#### 🔀 Ajuste Manual y Prioridades")
    st.caption("Elegí qué vehículo atiende a quién. **Si tildás '🌟 Forzar 1° Parada', el técnico irá ahí ni bien salga de la base.**")

    opciones_vehiculos = [f"Vehículo {i+1}" for i in range(cant_vehiculos)]
    
    df_para_editar = df_locales[['cliente_principal', 'nombre_fantasia', 'direccion', 'max_dias', 'Vehículo Asignado', 'Forzar_Primera']].copy()
    
    df_editado = st.data_editor(
        df_para_editar,
        column_config={
            "cliente_principal": st.column_config.TextColumn("Nº Cliente", disabled=True),
            "nombre_fantasia": st.column_config.TextColumn("Fantasía", disabled=True),
            "direccion": st.column_config.TextColumn("Ubicación", disabled=True),
            "max_dias": st.column_config.NumberColumn("Demora (Días)", disabled=True),
            "Vehículo Asignado": st.column_config.SelectboxColumn(
                "Vehículo Asignado", options=opciones_vehiculos, required=True
            ),
            "Forzar_Primera": st.column_config.CheckboxColumn("🌟 Forzar 1° Parada", default=False)
        },
        hide_index=True,
        use_container_width=True,
        key="tabla_ajuste_vehiculos"
    )

    df_locales['Vehículo Asignado'] = df_editado['Vehículo Asignado']
    df_locales['Forzar_Primera'] = df_editado['Forzar_Primera']

    # --- 3. BOTÓN FINAL ---
    st.divider()
    if st.button("⚡ Calcular Secuencias Óptimas y Despachar", type="primary"):
        with st.spinner("Trazando las rutas para los vehículos configurados..."):
            
            if origen_personalizado.strip() == config_actual["depot_address"].strip():
                depot_lat, depot_lng = config_actual["depot_coords"]
                direccion_origen_final = config_actual["depot_address"]
            else:
                lat_or, lng_or, f_addr_or, exito_or = geocodificar_google(origen_personalizado, config_actual['provincia'])
                if exito_or:
                    depot_lat, depot_lng = lat_or, lng_or
                    direccion_origen_final = f_addr_or
                else:
                    depot_lat, depot_lng = config_actual["depot_coords"]
                    direccion_origen_final = config_actual["depot_address"]
            
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
                            row_dist.append(int(c * 6371000 * 1.35))
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

                # HACK MATEMÁTICO PARA FORZAR LA PRIMERA PARADA
                priority_nodes = np.where(df_grupo['Forzar_Primera'] == True)[0]
                if len(priority_nodes) > 0:
                    first_priority_node = int(priority_nodes[0]) + 1
                    solver = routing.solver()
                    start_index = routing.Start(0)
                    solver.Add(routing.NextVar(start_index) == first_priority_node)

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
            cols = st.columns(len(vehiculos_unicos))
            
            for idx_col, v_nombre in enumerate(sorted(vehiculos_unicos)):
                grupo_df = df_locales[df_locales['Vehículo Asignado'] == v_nombre]
                sub_df_ordenado, km_v = optimizar_secuencia_grupo(grupo_df)
                
                coords_ordenadas = sub_df_ordenado.apply(lambda row: f"{row['lat']},{row['lng']}", axis=1).tolist()
                
                rutas_links = []
                tamano_bloque = 9
                
                for i in range(0, len(coords_ordenadas), tamano_bloque):
                    bloque_coords = coords_ordenadas[i:i+tamano_bloque]
                    
                    if i == 0:
                        origen_ruta = direccion_origen_final
                    else:
                        origen_ruta = coords_ordenadas[i-1]
                        
                    if i + tamano_bloque >= len(coords_ordenadas):
                        destino_ruta = direccion_origen_final
                    else:
                        destino_ruta = bloque_coords[-1]
                        bloque_coords = bloque_coords[:-1]
                        
                    waypoints_str = "|".join([urllib.parse.quote(c) for c in bloque_coords])
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

                    st.markdown("**Secuencia Óptima:**")
                    paso = 1
                    texto_paradas_wa = ""
                    
                    # --- BUCLE DE ARMADO DE TEXTOS ---
                    for _, local in sub_df_ordenado.iterrows():
                        texto_fantasia = f" ({local['nombre_fantasia']})" if str(local['nombre_fantasia']).strip() else ""
                        texto_entre = f"🛣️ *Entre calles:* {local['entre_calles']}%0A" if str(local['entre_calles']).strip() else ""
                        texto_vip = " 🌟 *(Prioridad)*" if local['Forzar_Primera'] else ""
                        
                        tel_crudo = str(local['detalles'][0]['tel']).replace('.0', '').strip()
                        texto_tel_wa = f"📞 *Teléfono:* {tel_crudo}%0A" if tel_crudo and tel_crudo.lower() != 'nan' else ""
                        
                        # 1. LO QUE SE VE EN LA PÁGINA WEB
                        st.markdown(f"**{paso}. {local['cliente_principal']}{texto_fantasia}{texto_vip}**")
                        st.caption(f"📍 {local['direccion']}")
                        if str(local['entre_calles']).strip():
                            st.caption(f"🛣️ Entre calles: {local['entre_calles']}")
                        if tel_crudo and tel_crudo.lower() != 'nan':
                            st.caption(f"📞 Teléfono: {tel_crudo}")
                        
                        # 2. EL ENCABEZADO DEL MENSAJE DE WHATSAPP
                        texto_paradas_wa += f"%0A*{paso}. {local['cliente_principal']}{texto_fantasia}{texto_vip}*%0A📍 {local['direccion']}%0A{texto_entre}{texto_tel_wa}"

                        # 3. EL DETALLE DE LAS ÓRDENES
                        for det in local['detalles']:
                            # Pantalla web
                            st.write(
                                f"↳ Puesto: `{det['puesto']}` | Orden: `{det['orden']}` | "
                                f"Activo: `{det['activo']}` | Obs: `{det['obs']}`"
                            )
                            # Mensaje de WhatsApp
                            texto_paradas_wa += (
                                f"   🔸 *Puesto:* {det['puesto']} | *Orden:* {det['orden']}%0A"
                                f"   🔸 *Activo:* {det['activo']} | *Obs:* {det['obs']}%0A%0A"
                            )

                        for orig_idx in local['indices_originales']:
                            df.loc[orig_idx, 'Vehículo Asignado'] = v_nombre
                            df.loc[orig_idx, 'Link de Ruta'] = str(rutas_links[0])
                            df.loc[orig_idx, 'Prioridad VIP'] = 'Sí' if local['Forzar_Primera'] else 'No'

                        st.write("---")
                        paso += 1

                    for idx_link, link_ruta in enumerate(rutas_links):
                        st.link_button(f"🗺️ Abrir Ruta (Parte {idx_link + 1})" if len(rutas_links)>1 else "🗺️ Abrir Hoja de Ruta", link_ruta)
                    
                    texto_links_wa = "🔗 *Link(s) de Ruta:*%0A"
                    for link_ruta in rutas_links:
                        texto_links_wa += f"{urllib.parse.quote(link_ruta)}%0A%0A"
                    
                    msg_wa = (
                        f"🚚 *HOJA DE RUTA - {v_nombre.upper()}*%0A"
                        f"📊 *Visitas:* {len(sub_df_ordenado)} puntos%0A"
                        f"----------------------------------------%0A"
                        f"📋 *DETALLE:*%0A{texto_paradas_wa}"
                        f"----------------------------------------%0A{texto_links_wa}"
                    )
                    st.link_button("💬 Enviar por WhatsApp", f"https://api.whatsapp.com/send?text={msg_wa}")

            st.divider()
            output_name = "ordenes_despachadas.xlsx"
            df.to_excel(output_name, index=False)
            with open(output_name, "rb") as file:
                st.download_button(
                    label="📥 Descargar Excel Final",
                    data=file,
                    file_name="ordenes_despachadas.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
