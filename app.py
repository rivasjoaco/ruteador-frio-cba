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
        "provincia": "Córdoba"
    },
    "Mendoza (Centro 0962)": {
        "codigos": ["0962", "962"],
        "depot_address": "Alsina 2336, M5501 Godoy Cruz, Mendoza, Argentina",
        "depot_coords": (-32.923, -68.835),
        "provincia": "Mendoza"
    }
}

MINUTOS_POR_PARADA = 25
MAX_HORAS_JORNADA = 7.5

st.title("🚚 Torre de Control - Servicio Técnico Frío")
st.subheader("Ruteador Multirregión (Motor de Geolocalización Google Maps)")

# Campo para ingresar la API Key de Google Maps
st.sidebar.header("⚙️ Configuración Google Maps")
google_api_key = st.sidebar.text_input("Ingrese Google Maps API Key:", type="password")

if not google_api_key:
    st.info("👈 **Para comenzar, ingresá tu Google Maps API Key en el menú lateral izquierdo.**")
    st.stop()

# Inicializar cliente de Google Maps
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

    # Columnas principales por posición
    col_orden = df.columns[0]      # Col A: Orden
    col_cliente = df.columns[2]    # Col C: Cliente
    col_direccion = df.columns[3]  # Col D: Dirección
    col_telefono = df.columns[5]   # Col F: Teléfono
    col_cp = df.columns[6]         # Col G: CP
    col_texto_breve = df.columns[7]# Col H: Texto breve / Obs
    col_activo = df.columns[9]     # Col J: Activo Fijo
    col_centro = df.columns[10]    # Col K: Centro

    opcion_region = st.selectbox("Seleccione el Centro / Región a procesar:", list(CENTROS_CONFIG.keys()))
    config_actual = CENTROS_CONFIG[opcion_region]

    codigos_centro = [str(c) for c in config_actual["codigos"]]
    df_filtrado = df[df[col_centro].astype(str).str.strip().isin(codigos_centro)].copy()

    if df_filtrado.empty:
        st.warning(f"⚠️ No se encontraron órdenes correspondientes a {opcion_region} en este archivo.")
        st.stop()

    st.success(f"📊 Se encontraron **{len(df_filtrado)} órdenes en total** asociadas a **{opcion_region}**.")

    # Limpieza de dirección para quitar ceros a la izquierda de SAP
    def limpiar_direccion(dir_raw):
        dir_str = str(dir_raw).strip()
        dir_str = re.sub(r'\b0+(\d+)', r'\1', dir_str)
        return dir_str

    df_filtrado['direccion_limpia'] = df_filtrado[col_direccion].apply(limpiar_direccion)

# Geolocalización robusta con Google Maps
    def geocodificar_google(dir_texto, cp_texto):
        prov = config_actual['provincia']
        # Consulta estructurada limpia para Google
        query = f"{dir_texto}, {prov}, Argentina"
        try:
            # Forzamos la región Argentina
            geocode_result = gmaps.geocode(query, region='ar')
            if geocode_result and len(geocode_result) > 0:
                res = geocode_result[0]
                location = res['geometry']['location']
                formatted_address = res.get('formatted_address', query)
                return location['lat'], location['lng'], formatted_address, True
        except Exception as e:
            pass
        return config_actual["depot_coords"][0], config_actual["depot_coords"][1], dir_texto, False

    # Memoria en sesión para coordenadas
    if "coords_cache_gmaps" not in st.session_state:
        st.session_state.coords_cache_gmaps = {}
    if "direcciones_editadas" not in st.session_state:
        st.session_state.direcciones_editadas = {}
    if "direcciones_descartadas" not in st.session_state:
        st.session_state.direcciones_descartadas = set()

    st.divider()
    st.markdown("### 🔍 Validación con Google Maps")

    direcciones_unicas = df_filtrado[['direccion_limpia', col_cp]].drop_duplicates()
    no_encontradas = []

    for _, row_dir in direcciones_unicas.iterrows():
        d_orig = row_dir['direccion_limpia']
        cp_val = row_dir[col_cp]
        
        if d_orig in st.session_state.direcciones_descartadas:
            continue

        d_actual = st.session_state.direcciones_editadas.get(d_orig, d_orig)
        cache_key = f"{d_actual}_{config_actual['provincia']}"

        if cache_key not in st.session_state.coords_cache_gmaps:
            lat, lng, formatted_addr, exito = geocodificar_google(d_actual, cp_val)
            st.session_state.coords_cache_gmaps[cache_key] = (lat, lng, formatted_addr, exito)
        
        lat, lng, formatted_addr, exito = st.session_state.coords_cache_gmaps[cache_key]
        
        if not exito:
            no_encontradas.append((d_orig, d_actual))

    df_filtrado_activo = df_filtrado[~df_filtrado['direccion_limpia'].isin(st.session_state.direcciones_descartadas)].copy()

    if no_encontradas:
        st.warning(f"⚠️ **Atención:** Hay **{len(no_encontradas)} dirección(es)** que requieren revisión manual:")
        for d_orig, d_actual in no_encontradas:
            with st.container():
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
        st.success("✅ ¡Google Maps ubicó correctamente todas las direcciones de la planilla!")

    # Botón principal de Ruteo
    if st.button("⚡ Optimizar y Despachar Flota", type="primary"):
        if df_filtrado_activo.empty:
            st.error("No hay direcciones válidas para procesar el ruteo.")
            st.stop()

        with st.spinner(f"Agrupando locales y calculando rutas óptimas en {config_actual['provincia']}..."):
            
            grupos_locales = []
            for dir_orig, group in df_filtrado_activo.groupby('direccion_limpia'):
                cache_key = f"{dir_orig}_{config_actual['provincia']}"
                
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
                        'obs': str(row[col_texto_breve])
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
            total_locales_unicos = len(df_locales)

            # Capacidad de Flota
            PARADAS_MAX_POR_VEHICULO = 8
            CAPACIDAD_TOTAL_FLOTA = PARADAS_MAX_POR_VEHICULO * 2

            if total_locales_unicos > CAPACIDAD_TOTAL_FLOTA:
                st.info(
                    f"💡 **AVISO DE CAPACIDAD DE FLOTA:**\n\n"
                    f"Ingresaron **{len(df_filtrado_activo)} órdenes válidas** repartidas en **{total_locales_unicos} direcciones únicas**.\n\n"
                    f"Saliendo con **2 vehículos**, se cubrirán las primeras **{CAPACIDAD_TOTAL_FLOTA} ubicaciones prioritarias** de hoy."
                )
                df_locales = df_locales.iloc[:CAPACIDAD_TOTAL_FLOTA].copy()

            # Asignación de vehículos (K-Means)
            usar_dos = len(df_locales) >= 6
            depot_lat, depot_lng = config_actual["depot_coords"]

            if usar_dos:
                coords_matrix = df_locales[['lat', 'lng']].values
                kmeans = KMeans(n_clusters=2, random_state=42, n_init=10).fit(coords_matrix)
                df_locales['cluster'] = kmeans.labels_
                df_locales['Vehículo Asignado'] = df_locales['cluster'].map({0: 'Vehículo 1 (Zona A)', 1: 'Vehículo 2 (Zona B)'})
            else:
                df_locales['Vehículo Asignado'] = 'Vehículo 1'

            # Solver OR-Tools
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

            st.divider()
            st.info(f"💡 **DESPACHO REGIONAL ({config_actual['provincia'].upper()}):** Se asignaron **{len(vehiculos_unicos)} VEHÍCULO(S)** saliendo de `{config_actual['depot_address']}`.")

            cols = st.columns(len(vehiculos_unicos))
            origen_encoded = urllib.parse.quote(config_actual["depot_address"])

            for idx_col, v_nombre in enumerate(sorted(vehiculos_unicos)):
                grupo_df = df_locales[df_locales['Vehículo Asignado'] == v_nombre]
                
                sub_df_ordenado, km_v = optimizar_secuencia_grupo(grupo_df)
                
                direcciones_ordenadas = sub_df_ordenado['direccion'].tolist()
                waypoints_encoded = "|".join([urllib.parse.quote(d) for d in direcciones_ordenadas])
                link_maps = f"https://www.google.com/maps/dir/?api=1&origin={origen_encoded}&destination={origen_encoded}&waypoints={waypoints_encoded}&travelmode=driving"
                
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
                        st.write(f"**{paso}.** **{local['cliente_principal']}** - {local['direccion']} *(Órdenes: {cant_ord})*")
                        
                        texto_paradas_wa += f"%0A*{paso}. {local['cliente_principal']} - {local['direccion']}*%0A"
                        if cant_ord > 1:
                            texto_paradas_wa += f"   ⚠️ *Este local tiene {cant_ord} órdenes/servicios acumulados:*%0A"

                        for det in local['detalles']:
                            texto_paradas_wa += (
                                f"   • Orden #{det['orden']} | Activo: {det['activo']}%0A"
                                f"     Tel: {det['tel']} | Obs: {det['obs']}%0A"
                            )

                            for orig_idx in local['indices_originales']:
                                df.loc[orig_idx, 'Vehículo Asignado'] = v_nombre
                                df.loc[orig_idx, 'Estado'] = 'En Ruta'
                                df.loc[orig_idx, 'Link de Ruta'] = str(link_maps)

                        paso += 1

                    st.link_button("🗺️ Abrir Hoja de Ruta en Google Maps", link_maps)
                    
                    msg_wa = (
                        f"🚚 *HOJA DE RUTA - {v_nombre.upper()} ({config_actual['provincia'].upper()})*%0A"
                        f"📍 *Punto de Salida/Retorno:* {config_actual['depot_address']}%0A"
                        f"📊 *Ubicaciones a Visitar:* {len(sub_df_ordenado)} puntos%0A"
                        f"----------------------------------------%0A"
                        f"📋 *DETALLE DE PUNTOS Y ACTIVOS:*%0A"
                        f"{texto_paradas_wa}%0A"
                        f"----------------------------------------%0A"
                        f"🔗 *Link de Google Maps Ordenado:*%0A{urllib.parse.quote(link_maps)}"
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
